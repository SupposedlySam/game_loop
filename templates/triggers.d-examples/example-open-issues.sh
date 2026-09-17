#!/usr/bin/env bash
# EXAMPLE stepback/session_start attachment — the open work queue, printed at the moment the loop
# looks up from the work.
#
# WHY THIS SHIPS AS AN EXAMPLE RATHER THAN LIVING IN A CHAT MESSAGE. Audited 2026-09-17: three
# separate projects on this machine had independently built this same attachment, each from a
# broadcast rather than from a file. They had drifted — one was 70 lines to another's 50, one had
# lost the acknowledgement mechanism entirely, so a reply its author had decided needed nothing
# re-reported forever. A copy taken from a message never receives the original's later fixes. A
# copy taken from here upgrades when the payload does.
#
# THE PROBLEM IT SOLVES IS NOT "I FORGET TO CHECK". It is that FILING FEELS LIKE PROGRESS. An issue
# you opened reads as handled; a standing instruction to work the queue decays exactly as fast as
# memory does, and nothing ever asks again.
#
# IT REPORTS AND NEVER BLOCKS. Attached at `stepback` or `session_start`, both of which are
# announcement moments — the verb has already happened and nothing this prints can change it. Do not
# attach it to `stop`, where a non-zero exit blocks a turn-end: a queue being non-empty is the
# ordinary state of a project, not a reason to refuse.
set -uo pipefail
cat >/dev/null            # consume the payload; nothing here needs it

command -v gh >/dev/null 2>&1 || { echo "gh not on PATH — cannot report the work queue" >&2; exit 1; }

# THE REPO IS ASKED FOR, NOT HARDCODED, which is the one change that makes this copyable. Every
# instance of this script found in the audit named its own repo in a string, so the file could not
# move between projects without an edit somebody had to remember.
REPO="${GL_ISSUE_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)}"
[ -n "$REPO" ] || { echo "no repo: not a GitHub checkout, and GL_ISSUE_REPO is unset" >&2; exit 1; }

rows="$(gh issue list --repo "$REPO" --state open --limit 60 --json number,title,labels 2>&1)" || {
  echo "could not reach the issue tracker for $REPO: $rows" >&2; exit 1; }

GL_ROWS="$rows" GL_REPO="$REPO" python3 <<'PY'
import json, os
rows = json.loads(os.environ["GL_ROWS"])

# LABELS THAT MEAN "A HUMAN OWES AN ANSWER" ARE COUNTED SEPARATELY, NOT LISTED BESIDE THE WORK.
# They are neither forgotten nor nagged about. Listing them next to actionable items trains the
# reader to skim the whole list, which is how a standing report stops being read at all.
WAITING = {"needs-input", "blocked", "context"}
waiting = [r for r in rows if any(l.get("name") in WAITING for l in (r.get("labels") or []))]
actionable = sorted((r for r in rows if r not in waiting), key=lambda r: r["number"])

if not rows:
    print("no open issues — the queue is genuinely empty")
else:
    print("%d issue(s) actionable, %d waiting on a human decision" % (len(actionable), len(waiting)))
    for r in actionable[:12]:
        print("   #%-5s %s" % (r["number"], r["title"][:88]))
    if len(actionable) > 12:
        print("   ... and %d more" % (len(actionable) - 12))
    if waiting:
        print("   -- waiting on a human (do not guess these): "
              + ", ".join("#%s" % r["number"] for r in waiting))

# SAY WHAT IT DID NOT LOOK AT. A queue report that names only what it found reads as complete.
print("   (open issues only, newest 60. A CLOSED issue with a new reply is a different check.)")
PY
