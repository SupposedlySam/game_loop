#!/usr/bin/env bash
# guard-writes — deny any mutation outside this repo. A PreToolUse hook (LOUD rung: fails at the
# point of misuse, with a reason). This is the guardrail that makes unattended running safe: an agent
# left alone all night cannot touch anything outside the project it was pointed at.
#
# WHY THIS CANNOT LIVE IN CLAUDE.md: "don't touch other projects" written as an instruction is a
# promise, and the whole premise of game_loop is that promises break under long sessions and compaction.
# A hook holds whether or not the agent remembers it.
#
# THE MODEL: an ALLOWLIST, not a denylist. Writes are permitted only under the repo, the OS temp dir,
# this project's agent-memory dir, and anything in config.json -> allow_write_roots. Everything else
# is denied by default. A denylist ("block ~/dev, ~/.ssh, ...") defaults to UNPROTECTED and silently
# misses whatever nobody remembered to list; an allowlist defaults to PROTECTED.
#
# SCOPE — what this DOES and does NOT catch (a guard that overstates its reach buys false confidence):
#   DOES: Write/Edit/NotebookEdit whose target resolves outside the allow roots.
#   DOES: Bash mutators (rm/mv/cp/mkdir/chmod/... , shell redirects, git writes, sed -i) whose
#         resolved target is outside the allow roots. Paths resolved by realpath, `cd` tracked across
#         segments, every offending path collected (not just the first).
#   DOES: Bash invoking a configured deploy/publish verb, anywhere (config.json -> deploy_verbs).
#   DOES NOT: catch mutations made via MCP tools, an interpreter one-liner (`python3 -c 'os.remove(..)'`),
#             a path built from a shell variable (`rm $TARGET/x`), or a script that mutates without
#             naming the path on the command line. Those need tool-level matching this does not do.
#             Do not read silence here as safety.
#
# THE ESCAPE HATCH IS THE HUMAN, deliberately. There is no env-var override — a guard the agent can
# switch off is not a guard. A single mutation outside the repo is unlocked only by
# `game_loop authorize --path <prefix> --reason "<their words>"`, which is single-use and logged forever.

set -uo pipefail
payload=$(cat)

GAMELOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"    # .game_loop/
REPO="${CLAUDE_PROJECT_DIR:-$(dirname "$GAMELOOP_DIR")}"
CONFIG_F="$GAMELOOP_DIR/config.json"

REPO_REAL=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$REPO" 2>/dev/null)
SLUG=$(python3 -c 'import re,sys; print(re.sub(r"[^a-zA-Z0-9]", "-", sys.argv[1]))' "$REPO_REAL" 2>/dev/null)

deny() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}\n' \
    "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  exit 0
}

tool=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_name",""))' 2>/dev/null)

case "$tool" in
  Write|Edit|NotebookEdit)
    fp=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' 2>/dev/null)
    [ -z "$fp" ] && exit 0
    allowed=$(REPO_REAL="$REPO_REAL" SLUG="$SLUG" CONFIG_F="$CONFIG_F" FP="$fp" python3 <<'PY'
import json, os
repo = os.environ["REPO_REAL"]
home = os.path.expanduser("~")
allow = [repo, "/tmp", "/private/tmp", "/var/folders",
         os.path.join(home, ".claude", "projects", os.environ["SLUG"])]
try:
    with open(os.environ["CONFIG_F"]) as f:
        allow += [os.path.expanduser(p) for p in (json.load(f).get("allow_write_roots") or [])]
except (OSError, ValueError):
    pass
allow = [os.path.realpath(p) for p in allow]
real = os.path.realpath(os.environ["FP"])
print("yes" if any(real == a or real.startswith(a + os.sep) for a in allow) else "no")
PY
)
    [ "$allowed" = "yes" ] && exit 0
    deny "BLOCKED: write outside this repo → $fp

Everything outside this project is READ-ONLY by default (this is the guardrail that makes unattended
runs safe). If the repo genuinely needs that content, COPY it in and edit the copy. If the human has
explicitly authorized this path:  game_loop authorize --path <prefix> --reason \"<their words>\""
    ;;

  Bash)
    cmd=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null)
    [ -z "$cmd" ] && exit 0

    # 0. A commit is when a change becomes real. Refuse one whose owed checks (.game_loop/verify.yaml)
    #    have not run SINCE the change. No-op when verify.yaml is empty, so it costs nothing until you
    #    opt in. --no-verify skips it, out loud and on the record. Gates `git commit` only, not every
    #    write: a check per keystroke is ceremony that gets switched off.
    if printf '%s' "$cmd" | grep -qE '(^|[[:space:];&|])git[[:space:]]+commit' \
       && ! printf '%s' "$cmd" | grep -q -- '--no-verify'; then
      if ! "$GAMELOOP_DIR/bin/verify" --check >/tmp/.game_loop_verify 2>&1; then
        # ORDERING NOTE: this hook runs at PreToolUse, BEFORE the command body executes. Bundling
        # `verify` and `git commit` in ONE call can never pass — the check runs before your verify
        # line does. Run them as two separate calls.
        chained_hint=""
        if printf '%s' "$cmd" | grep -qE 'bin/verify|\bverify\b'; then
          chained_hint="
YOU CHAINED verify WITH THIS COMMIT IN ONE COMMAND. That can't work: this gate runs BEFORE the
command body, so your verify line hasn't executed yet. Run verify as a SEPARATE, EARLIER call."
        fi
        deny "$(cat /tmp/.game_loop_verify)

A green check from BEFORE your change is evidence about code that no longer exists.
Run ./.game_loop/bin/verify, or commit with --no-verify to skip it on the record.$chained_hint"
      fi
    fi

    # 1. Configured deploy/publish verbs — denied anywhere, no path needed.
    deploy_hit=$(CONFIG_F="$CONFIG_F" CMD="$cmd" python3 <<'PY'
import json, os, re
defaults = ["npm publish", "yarn publish", "pnpm publish", "twine upload",
            "gh release create", "docker push"]
verbs = list(defaults)
try:
    with open(os.environ["CONFIG_F"]) as f:
        verbs += (json.load(f).get("deploy_verbs") or [])
except (OSError, ValueError):
    pass
cmd = os.environ["CMD"]
for v in verbs:
    pat = r"(^|[\s;&|])" + r"\s+".join(re.escape(w) for w in v.split())
    if re.search(pat, cmd):
        print(v)
        break
PY
)
    if [ -n "$deploy_hit" ]; then
      deny "BLOCKED: deploy/publish verb '$deploy_hit'.

This is an irreversible, outward-facing action (a real publish/release/deploy). An unattended agent
does not fire these. If it is genuinely needed, escalate to the human — that is the only escape
hatch, by design. (Configured in .game_loop/config.json -> deploy_verbs.)"
    fi

    # 2. Mutation aimed OUTSIDE the allow roots, decided by RESOLVING PATHS — not matching names.
    offender=$(REPO_REAL="$REPO_REAL" SLUG="$SLUG" CONFIG_F="$CONFIG_F" python3 - "$payload" <<'PY'
import json, os, re, shlex, sys

payload = json.loads(sys.argv[1])
cmd = payload.get("tool_input", {}).get("command", "")
cwd = payload.get("cwd") or os.environ["REPO_REAL"]
home = os.path.expanduser("~")

allow = [os.environ["REPO_REAL"], "/tmp", "/private/tmp", "/var/folders",
         os.path.join(home, ".claude", "projects", os.environ["SLUG"])]
try:
    with open(os.environ["CONFIG_F"]) as f:
        allow += [os.path.expanduser(p) for p in (json.load(f).get("allow_write_roots") or [])]
except (OSError, ValueError):
    pass
allow = [os.path.realpath(p) for p in allow]

MUTATORS = {"rm", "rmdir", "touch", "mkdir", "chmod", "chown", "ln", "dd", "truncate", "tee"}
GIT_WRITES = {"commit", "push", "reset", "rebase", "checkout", "clean", "apply", "restore", "mv"}


def under(path, root):
    return path == root or path.startswith(root + os.sep)


# Standard character devices: discard sinks and the console/std streams. A redirect to one of these
# (e.g. `2>/dev/null`, `>/dev/stderr`) never writes a real out-of-repo file, so it must not be flagged.
# Matched on the LITERAL path — /dev/stdout & friends are symlinks realpath would resolve away.
STD_DEVICES = {"/dev/null", "/dev/zero", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty",
               "/dev/random", "/dev/urandom"}


def offends(raw, cwd):
    """Return the offending realpath, or None if this path is inside an allow root."""
    p = os.path.expanduser(raw.replace("$HOME", home))
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    if p in STD_DEVICES or p.startswith("/dev/fd/"):
        return None
    real = os.path.realpath(p)
    return None if any(under(real, a) for a in allow) else real


offenders = []
# Split on shell separators AND newlines. Omitting \n would collapse a multi-line command into one
# segment whose verb is its first token, so a mutating later line would never be checked.
for seg in re.split(r"&&|\|\||;|\||\n", cmd):
    try:
        argv = shlex.split(seg)
    except ValueError:
        argv = seg.split()
    if not argv:
        continue
    verb = os.path.basename(argv[0])
    args = argv[1:]
    pathish = [a for a in args if not a.startswith("-") and "&" not in a and a not in (">", ">>")]

    if verb == "cd" and pathish:
        nxt = os.path.expanduser(pathish[0].replace("$HOME", home))
        cwd = nxt if os.path.isabs(nxt) else os.path.join(cwd, nxt)
        continue

    check = []
    if verb == "cp":
        check = pathish[-1:]                       # cp checks its DESTINATION only (reading is fine)
    elif verb == "mv":
        check = pathish                            # mv mutates source AND destination
    elif verb in MUTATORS:
        check = pathish
    elif verb == "sed" and "-i" in args:
        check = pathish
    elif verb == "git" and any(a in GIT_WRITES for a in args):
        check = pathish                            # catches `git -C <path> commit`
    for m in re.finditer(r">>?\s*([^\s;&|<>]+)", seg):
        check.append(m.group(1))                   # redirects mutate regardless of the verb

    for raw in check:
        bad = offends(raw, cwd)
        if bad:
            offenders.append(bad)

for o in dict.fromkeys(offenders):
    print(o)
PY
)

    if [ -n "$offender" ]; then
      # A human-authorized, single-use exception (`game_loop authorize`). Consulted here and CONSUMED,
      # so one authorization buys one mutation. No env override: it cannot be set without writing a
      # permanent log entry carrying the human's own words.
      consumed=$(OFFENDER="$offender" GAMELOOP_DIR="$GAMELOOP_DIR" python3 <<'PY'
import json, os, sys, datetime
state_f = os.path.join(os.environ["GAMELOOP_DIR"], "state.json")
log_f = os.path.join(os.environ["GAMELOOP_DIR"], "log.jsonl")
off = os.environ["OFFENDER"]
try:
    with open(state_f) as f:
        st = json.load(f)
except (OSError, ValueError):
    sys.exit(0)
for a in st.get("authorized", []):
    if a.get("uses_left", 0) <= 0:
        continue
    root = a.get("path", "")
    if off == root or off.startswith(root + os.sep):
        a["uses_left"] -= 1
        try:
            with open(state_f, "w") as f:
                json.dump(st, f, indent=2); f.write("\n")
            with open(log_f, "a") as f:
                f.write(json.dumps({
                    "t": datetime.datetime.now().isoformat(timespec="seconds"),
                    "kind": "authorized_write", "path": off,
                    "reason": a.get("reason"), "uses_left": a["uses_left"]}) + "\n")
        except OSError:
            sys.exit(0)
        print("yes")
        break
PY
)
      [ "$consumed" = "yes" ] && exit 0
      deny "BLOCKED: mutating command targets a path outside this repo → $offender

Everything outside this project is READ-ONLY by default. READING elsewhere is fine, and so is copying
OUT of it: \`cp <their path> <repo path>\` is allowed. Copy what you need in and work on the copy.

If the human has explicitly authorized this specific path, record their words and try again:
  game_loop authorize --path <prefix> --reason \"<their exact words>\"
One authorization, one mutation, logged permanently. That is the only escape hatch, by design."
    fi
    ;;
esac

exit 0
