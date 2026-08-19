#!/usr/bin/env bash
# STOP-trigger: refuse a turn-end where this session did substantive work and cited nothing.
#
# THE FAILURE THIS EXISTS FOR. `claim --read` is the one rule that keeps an assertion about
# external reality tied to a file somebody actually opened. It is easy to read past: the counter
# sits quietly on the status line, work gets done, a learning gets hardened, and the turn ends
# without a single claim behind anything that was told to a human or another agent. The rule was
# never missing — this just makes it a command instead of something you have to remember to check.
#
# WHAT IT ASKS: if the session hardened a learning or recorded a phase transition since the last
# mandate was bound — i.e. real work happened under it — then at least one claim must exist. One
# claim clears it, in either direction: `--outcome refuted` costs the same as asserting, so being
# wrong is as recordable as being right.
#
# WHEN IT STAYS QUIET, which is most turns:
#   * no mandate is currently bound (never set, or already cleared/parked) — nothing is owed
#   * a read-only or conversational turn under a mandate (no harden, no phase transition)
#   * any session with at least one claim, resolved OR refuted, since the mandate was bound
#   * no log yet
#
# WHAT IT CANNOT SEE: whether the claims that exist actually cover the assertions made. It counts,
# it does not read — a session that cites one file and then asserts ten unrelated things passes
# this. It also cannot see assertions made outside this log, e.g. through a chat tool or another
# agent's own channel.
#
# FIXTURE SHAPE 1 (see ../../test/trigger_fixtures.py): a synthetic $GAME_LOOP_ROOT/log.jsonl,
# using only kinds `game_loop kinds` confirms this codebase actually writes.
#
# CONTRACT: exit 0 = turn may end. non-zero = BLOCKED, stderr goes back to the model. Fails open on
# anything it cannot answer — a guard must never block its own fix.
set -uo pipefail

LOG="${GAME_LOOP_ROOT:-.game_loop}/log.jsonl"
[ -f "$LOG" ] || exit 0

python3 - "$LOG" "${GAME_LOOP_SESSION:-}" <<'PY'
import json, sys

log_path, raw_session = sys.argv[1], sys.argv[2]
sid = raw_session[:8] if raw_session else ''   # logline() stamps sid as SESSION[:8]
claims = hardens = transitions = 0
mandate_bound = False

for line in open(log_path, encoding='utf-8', errors='replace'):
    try:
        d = json.loads(line)
    except Exception:
        continue
    if sid and d.get('sid') and d['sid'] != sid:
        continue
    k = d.get('kind')
    if k == 'mandate_set':
        mandate_bound = True
        claims = hardens = transitions = 0   # a fresh mandate starts a fresh window
    elif k in ('mandate_clear', 'mandate_park'):
        mandate_bound = False                # nothing is owed once the mandate is gone
    elif k == 'claim':
        claims += 1
    elif k == 'harden':
        hardens += 1
    elif k == 'trans':
        transitions += 1

if not mandate_bound:
    sys.exit(0)                              # nothing was ever asked of this session, or it's over

work = hardens + transitions
if claims > 0 or work == 0:
    sys.exit(0)

out = sys.stderr
print("STOP REFUSED — this session did substantive work and sourced no claims since the mandate "
      "was bound.", file=out)
print(file=out)
print(f"  hardens + transitions: {work}    claims: {claims}", file=out)
print(file=out)
print("Cite the file before asserting anything about external reality. One claim clears this,", file=out)
print("in either direction:", file=out)
print("  game_loop claim --assert '..' --read <a real path you opened> --confidence '..'", file=out)
print("  game_loop claim --assert '..' --outcome refuted --evidence <the file that disproved it>", file=out)
sys.exit(1)
PY
exit $?
