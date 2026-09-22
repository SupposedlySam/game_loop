#!/usr/bin/env bash
# STOP-trigger: hold this session to ITS OWN pull request, and to nobody else's.
#
# WHY THIS EXISTS AS A SHIPPED EXAMPLE (game_loop#130). The README beside this file says a trigger
# that BLOCKS must select on something THIS SESSION owns, and for a while it said only that. A rule
# with no mechanism is how the reported defect happened: a consumer's gate reached for the obvious
# selector, `gh pr list --author "@me"`, and `@me` is the ACCOUNT. Two sessions sharing one login —
# the normal case — and the gate fired on session B's turn-end naming session A's PR, offering as
# its only remedy a marker meaning "I did the work". Session B refused, correctly, under blocking
# pressure. Copy THIS shape instead of writing that one.
#
# WHAT IT ASKS: of the open PRs on this repo, is one of them headed by the branch THIS CHECKOUT IS
# STANDING ON? That is the discriminator which actually separated the two sessions in #130 — they
# shared a checkout but were on different branches, so `@me` could not tell them apart and the
# current branch could.
#
# WHAT IT CANNOT DO, said plainly rather than left to be discovered (INV6). Two sessions on the
# SAME branch of the SAME checkout are indistinguishable to this and to everything else game_loop
# has: there is no PR-to-session mapping in the tool, and nothing records which session opened
# which PR. This narrows "the account" to "this branch". It does not reach "this session", and a
# gate built on it must not claim to.
#
# SUBSTITUTE YOUR OWN OBLIGATION for the body below. The selection is the transferable part; the
# thing being demanded (a description, a changelog entry, a reviewer) is yours.
#
# FIXTURE SHAPE 3 (see ../../test/trigger_fixtures.py): stub `gh` on PATH and drive a throwaway git
# repo onto each branch in turn — the firing case and the quiet case differ only by which branch
# the checkout is standing on, which is exactly the property under test.
#
# CONTRACT: exit 0 = turn may end. non-zero = BLOCKED, stderr goes back to the model. Fails open on
# anything it cannot answer — a guard must never block its own fix.
set -uo pipefail

REPO="${GAME_LOOP_REPO:-.}"
cd "$REPO" 2>/dev/null || exit 0

command -v "${GH_BIN:-gh}" >/dev/null 2>&1 || exit 0   # no gh: cannot look, so do not block

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
[ $? -eq 0 ] && [ -n "$branch" ] || exit 0             # not a git repo, or git could not answer
[ "$branch" = "HEAD" ] && exit 0                       # detached: no branch to own a PR

# `--head <branch>` asks the tracker the session-scoped question directly, instead of listing the
# account's PRs and filtering afterwards. Same answer, and it cannot be misread later as "mine".
prs="$("${GH_BIN:-gh}" pr list --state open --head "$branch" --json number,title 2>/dev/null)"
[ $? -eq 0 ] || exit 0                                 # the tracker refused: could-not-look, not "none"
[ -n "$prs" ] || exit 0

num="$(printf '%s' "$prs" | sed -n 's/.*"number":[[:space:]]*\([0-9]*\).*/\1/p' | head -1)"
[ -n "$num" ] || exit 0                                # no open PR on this branch: quiet

# REPLACE THIS with whatever your project owes. Kept as a marker file so the example stays
# runnable, and deliberately the same shape #130 was about: a marker meaning "I did the work".
marker=".game_loop/pr-checked/$num"
[ -f "$marker" ] && exit 0

echo "STOP REFUSED — PR #$num is headed by '$branch', the branch this checkout is on." >&2
echo "It is this session's to account for. When you have done it:" >&2
echo "  mkdir -p .game_loop/pr-checked && touch $marker" >&2
echo >&2
echo "If that PR is NOT yours — two sessions can share a branch, and nothing here can tell —" >&2
echo "do NOT touch the marker: it means 'I did the work'. Tell the session that opened it." >&2
exit 1
