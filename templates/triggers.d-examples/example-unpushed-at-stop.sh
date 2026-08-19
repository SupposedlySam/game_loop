#!/usr/bin/env bash
# STOP-trigger: refuse a turn-end where HEAD carries commits its upstream has never seen.
#
# WHAT IT ASKS: is this branch ahead of `@{upstream}`? If so, work exists only on this machine —
# a crash, a wiped checkout, or another agent starting from the same remote will never see it.
#
# THE rc=128 TRAP THIS IS WRITTEN TO AVOID. `git rev-list @{u}..HEAD` fails outright — not with an
# empty answer — when there is no upstream configured, or this is not a git repository at all.
# `subprocess.run`/`$(...)` do not raise on a non-zero exit; a caller that reads only stdout sees an
# empty string in BOTH the "0 unpushed" case and the "could not tell" case, and treating them the
# same silently inverts this gate's own promise: a repo with no upstream configured would read as
# "fully pushed" and stay quiet forever. Every step below checks its exit code before trusting its
# output, and an unanswerable question fails OPEN (exit 0) rather than guessing either direction.
#
# FIXTURE SHAPE 2 (see ../../test/trigger_fixtures.py): a throwaway git repo, with and without an
# upstream configured, and with and without commits ahead of it.
#
# CONTRACT: exit 0 = turn may end. non-zero = BLOCKED, stderr goes back to the model. Fails open on
# anything it cannot answer — a guard must never block its own fix.
set -uo pipefail

REPO="${GAME_LOOP_REPO:-.}"
cd "$REPO" 2>/dev/null || exit 0

upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"
[ $? -eq 0 ] && [ -n "$upstream" ] || exit 0   # no upstream configured, or not a git repo: can't tell

ahead="$(git rev-list --count '@{u}..HEAD' 2>/dev/null)"
[ $? -eq 0 ] || exit 0                          # git could not answer: can't tell, not "zero"
[ "$ahead" -gt 0 ] 2>/dev/null || exit 0        # nothing ahead: quiet

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
echo "STOP REFUSED — $ahead commit(s) on '${branch:-HEAD}' are not on $upstream." >&2
echo "Push before ending the turn, or this work exists only on this checkout." >&2
exit 1
