#!/usr/bin/env bash
# test-e2e-verified-head-ancestor.sh — V5.6 e2e_verified_head ancestor + path filter
#
# Case C: e2e_pending + e2e_verified_head is ancestor + only .md diff → self-heal
# Case D: e2e_pending + e2e_verified_head is ancestor + .py diff → no self-heal
# Case E: e2e_pending + e2e_verified_head == HEAD (exact match, backward compat) → self-heal

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "e2e-verified-head-ancestor"

# ============================================================
# Case C: ancestor + only doc changes → self-heal
# ============================================================
section "Case C: e2e_verified_head ancestor + only .md → self-heal"
envC=$(create_test_env --slug "e2e-anc-c" --worktree --phase e2e_pending)
wtC="$envC/.claude/worktrees/e2e-anc-c"
sfC=$(state_file "$envC" "e2e-anc-c")

SEED_HEAD_C="$(git -C "$wtC" rev-parse HEAD)"

# commit a doc file in worktree (HEAD advances, only .md changed)
echo "doc update" > "$wtC/notes.md"
git -C "$wtC" add notes.md
git -C "$wtC" -c core.hooksPath=/dev/null commit -q -m "docs(test): [cr_id_skip] Doc update"
NEW_HEAD_C="$(git -C "$wtC" rev-parse HEAD)"

# set state: last_iter_head = new HEAD (so new_commit check won't fire)
# e2e_verified_head = seed (ancestor of new HEAD)
sed -i "s/^last_iter_head:.*/last_iter_head: \"$(git -C "$wtC" rev-parse --short HEAD)\"/" "$sfC"
printf 'e2e_verified_head: "%s"\n' "$SEED_HEAD_C" >> "$sfC"

resultC=$(run_hook "$envC")
phaseC=$(read_state_field "$sfC" "phase")
assert "Case C phase healed to active" "[ '$phaseC' = 'active' ]"
assert_stderr_contains "Case C stderr mentions e2e_verified" "$resultC" "e2e_verified"

# ============================================================
# Case D: ancestor + source code change → no self-heal
# ============================================================
section "Case D: e2e_verified_head ancestor + .py → no self-heal"
envD=$(create_test_env --slug "e2e-anc-d" --worktree --phase e2e_pending)
wtD="$envD/.claude/worktrees/e2e-anc-d"
sfD=$(state_file "$envD" "e2e-anc-d")

SEED_HEAD_D="$(git -C "$wtD" rev-parse HEAD)"

# commit a source file (HEAD advances, .py changed → NOT safe)
echo "print('hello')" > "$wtD/app.py"
git -C "$wtD" add app.py
git -C "$wtD" -c core.hooksPath=/dev/null commit -q -m "feat(test): [cr_id_skip] Source change"
NEW_HEAD_D="$(git -C "$wtD" rev-parse HEAD)"

sed -i "s/^last_iter_head:.*/last_iter_head: \"$(git -C "$wtD" rev-parse --short HEAD)\"/" "$sfD"
printf 'e2e_verified_head: "%s"\n' "$SEED_HEAD_D" >> "$sfD"

resultD=$(run_hook "$envD")
phaseD=$(read_state_field "$sfD" "phase")
assert "Case D phase stays e2e_pending" "[ '$phaseD' = 'e2e_pending' ]"
assert_ec "Case D hook EC=0 (L1 blocked)" "$resultD" 0

# ============================================================
# Case E: exact match (backward compat) → self-heal
# ============================================================
section "Case E: e2e_verified_head == HEAD → self-heal"
envE=$(create_test_env --slug "e2e-anc-e" --worktree --phase e2e_pending)
wtE="$envE/.claude/worktrees/e2e-anc-e"
sfE=$(state_file "$envE" "e2e-anc-e")

EXACT_HEAD_E="$(git -C "$wtE" rev-parse HEAD)"
printf 'e2e_verified_head: "%s"\n' "$EXACT_HEAD_E" >> "$sfE"

resultE=$(run_hook "$envE")
phaseE=$(read_state_field "$sfE" "phase")
assert "Case E phase healed to active" "[ '$phaseE' = 'active' ]"
assert_stderr_contains "Case E stderr mentions e2e_verified" "$resultE" "e2e_verified"

harness_report
