#!/usr/bin/env bash
# install game_loop into a target project: copy the .game_loop/ payload and wire the Claude Code hooks.
#
#   From a clone:   ./install.sh /path/to/your/project
#   One-liner:      curl -fsSL https://raw.githubusercontent.com/SupposedlySam/game_loop/main/install.sh | bash -s -- .
#
# The one-liner needs no local clone: the installer fetches the payload tarball from GitHub itself.
# Override the source with GAME_LOOP_REPO=owner/repo and GAME_LOOP_REF=branch|tag (default: main).
#
# Idempotent: re-running updates the scripts and re-merges the hooks without duplicating them. Your
# state.json, config.json, INVARIANTS.md, verify.yaml and LEDGER.md are NEVER overwritten once they
# exist — those are yours. Only the bin/ scripts are always refreshed.
set -euo pipefail

REPO="${GAME_LOOP_REPO:-SupposedlySam/game_loop}"
REF="${GAME_LOOP_REF:-main}"

# Locate the payload. Running from a clone, it sits next to this script. Piped through `curl | bash`
# there is no checkout — fetch the repo tarball into a temp dir and use that. `--strip-components=1`
# drops the archive's top folder (game_loop-<ref>/) so we never have to guess its name.
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -z "${SRC:-}" ] || [ ! -f "$SRC/.game_loop/bin/game_loop" ]; then
  command -v curl >/dev/null 2>&1 || { echo "curl is required to fetch game_loop." >&2; exit 1; }
  command -v tar  >/dev/null 2>&1 || { echo "tar is required to unpack game_loop." >&2; exit 1; }
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  echo "Fetching game_loop ($REPO@$REF) from GitHub…"
  mkdir -p "$TMP/payload"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" \
    | tar -xz -C "$TMP/payload" --strip-components=1
  SRC="$TMP/payload"
  if [ ! -f "$SRC/.game_loop/bin/game_loop" ]; then
    echo "Fetched archive did not contain the game_loop payload (looked in $SRC/.game_loop/bin/)." >&2
    exit 1
  fi
fi

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "usage: ./install.sh /path/to/your/project   (or pipe via curl: ... | bash -s -- .)" >&2
  exit 1
fi
TARGET="$(cd "$TARGET" && pwd)"

if [ "$TARGET" = "$SRC" ]; then
  echo "That is the game_loop repo itself — it already dogfoods game_loop. Point this at another project." >&2
  exit 1
fi

# Fresh install vs update: decided before we copy anything, so the closing summary can say which.
if [ -f "$TARGET/.game_loop/bin/game_loop" ]; then FRESH=0; else FRESH=1; fi

echo "Installing game_loop into: $TARGET"
mkdir -p "$TARGET/.game_loop/bin"

# Always refresh the executables — they are the tool. (flair.py is decoration and notify.py is
# paging; both are imported by the others and both degrade to no-ops.)
cp "$SRC/.game_loop/bin/game_loop" "$SRC/.game_loop/bin/watchdog" \
   "$SRC/.game_loop/bin/guard-writes.sh" "$SRC/.game_loop/bin/guard-writes-impl.sh" \
   "$SRC/.game_loop/bin/verify" "$SRC/.game_loop/bin/flair.py" "$SRC/.game_loop/bin/notify.py" \
   "$TARGET/.game_loop/bin/"
chmod +x "$TARGET/.game_loop/bin/game_loop" "$TARGET/.game_loop/bin/watchdog" \
         "$TARGET/.game_loop/bin/guard-writes.sh" "$TARGET/.game_loop/bin/guard-writes-impl.sh" \
         "$TARGET/.game_loop/bin/verify"
echo "  $([ "$FRESH" = 1 ] && echo copied || echo refreshed)  .game_loop/bin/ (game_loop, watchdog, guard-writes.sh + -impl, verify, flair.py, notify.py)"

# Seed the user-owned files only if absent — never clobber their config or notes.
# $2 overrides the source path (defaults to .game_loop/$1) for files that ship from templates/.
seed() {
  local dest="$1" src="${2:-.game_loop/$1}"
  if [ ! -e "$TARGET/.game_loop/$dest" ]; then
    cp "$SRC/$src" "$TARGET/.game_loop/$dest"
    echo "  seeded  .game_loop/$dest"
  else
    echo "  kept    .game_loop/$dest (already present)"
  fi
}
seed config.json
seed INVARIANTS.md
# verify.yaml ships EMPTY — the target has no test/run.py. This repo's OWN .game_loop/verify.yaml carries
# game_loop's dogfooding rules, which would (wrongly) fire on the target's first commit; ship the blank one.
seed verify.yaml templates/verify.yaml
seed LEDGER.md

# Set project_name in a freshly-seeded config to the target's directory name.
python3 - "$TARGET" <<'PY'
import json, os, sys
target = sys.argv[1]
cf = os.path.join(target, ".game_loop", "config.json")
try:
    with open(cf) as f:
        c = json.load(f)
except (OSError, ValueError):
    sys.exit(0)
if c.get("project_name") in (None, "", "game_loop"):
    c["project_name"] = os.path.basename(target)
    with open(cf, "w") as f:
        json.dump(c, f, indent=2)
        f.write("\n")
PY

# Merge the hooks into .claude/settings.json (create it if missing), idempotently.
mkdir -p "$TARGET/.claude"
python3 - "$SRC/templates/settings.hooks.json" "$TARGET/.claude/settings.json" <<'PY'
import json, os, sys
block_f, settings_f = sys.argv[1], sys.argv[2]
with open(block_f) as f:
    block = json.load(f)["hooks"]
try:
    with open(settings_f) as f:
        settings = json.load(f)
except (OSError, ValueError):
    settings = {}
hooks = settings.setdefault("hooks", {})


def commands(entry):
    return [h.get("command", "") for h in entry.get("hooks", [])]


# Warn about pre-existing NON-game_loop hooks on the events we manage. A stray Stop hook (e.g. an old
# .loop harness) will run ALONGSIDE ours and fight over turn-ends — the merge can't tell them apart, so
# we surface them and let the human delete the old one by hand. (game_loop's own hooks route through
# .game_loop/bin/, so that substring is how we recognize ours.)
foreign = []
for event in ("Stop", "PreToolUse"):
    for entry in hooks.get(event, []):
        for cmd in commands(entry):
            if cmd and ".game_loop/" not in cmd:
                foreign.append((event, cmd))
if foreign:
    print("  ⚠ found existing non-game_loop hooks on managed events — these run ALONGSIDE game_loop's")
    print("    and can fight over turn-ends. Delete the stale ones (e.g. an old .loop harness) by hand:")
    for event, cmd in foreign:
        print(f"      {event}: {cmd}")

for event, new_entries in block.items():
    arr = hooks.setdefault(event, [])
    existing_cmds = {c for e in arr for c in commands(e)}
    for entry in new_entries:
        # de-dupe on the actual command strings so re-installing never stacks duplicates
        if any(c in existing_cmds for c in commands(entry)):
            continue
        arr.append(entry)
        existing_cmds.update(commands(entry))

with open(settings_f, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
print("  merged  .claude/settings.json (PreToolUse guard + limitgate + Stop gate + watchdog)")

# The statusline is the ONLY place Claude Code exposes subscription rate limits, so it is the tap
# that feeds the limitgate and the watchdog's limit-park. Set it only when the project has none —
# a statusline is the user's front yard, and clobbering theirs to install a tap earns a rip-out.
GL_STATUSLINE = ('gl="${CLAUDE_PROJECT_DIR:-.}/.game_loop/bin/game_loop"; '
                 'if [ -x "$gl" ]; then exec "$gl" statusline; else cat >/dev/null; fi')
sl = settings.get("statusLine")
if not sl:
    settings["statusLine"] = {"type": "command", "command": GL_STATUSLINE, "refreshInterval": 60}
    with open(settings_f, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print("  set     statusLine (usage-limit tap → .game_loop/limits.json + one-row display)")
elif ".game_loop/bin/game_loop" not in json.dumps(sl):
    print("  ⚠ statusLine already configured and left alone — the usage-limit features need its")
    print("    data tap. Chain it from your own script (append this line, passing stdin through):")
    print('      tee >("${CLAUDE_PROJECT_DIR:-.}"/.game_loop/bin/game_loop statusline >/dev/null) | <your script>')
    print("    or replace your statusLine command with: " + GL_STATUSLINE)
PY

# Ignore the runtime files.
GI="$TARGET/.game_loop/.gitignore"
if [ ! -e "$GI" ]; then
  cat > "$GI" <<'EOF'
state.json
sessions/
log.jsonl
verified.json
probe/
*.pid
.state.*.tmp
notify.json
limits.json
.limits.*.tmp
.limits.lock
HANDOFF.md
EOF
  echo "  wrote   .game_loop/.gitignore"
else
  if ! grep -q '^sessions/$' "$GI"; then
    echo "sessions/" >> "$GI"
    echo "  updated .game_loop/.gitignore (+ sessions/ — per-session state)"
  fi
  # notify.json holds a Slack credential — it must be ignored BEFORE anyone writes it.
  if ! grep -q '^notify.json$' "$GI"; then
    printf 'notify.json\nlimits.json\n.limits.*.tmp\n.limits.lock\nHANDOFF.md\n' >> "$GI"
    echo "  updated .game_loop/.gitignore (+ notify.json, limits.json, HANDOFF.md)"
  fi
fi

# Per-session migration honesty: an active mandate in the OLD repo-global state.json gates nobody
# once state is per-session. Its owning session would just go quiet — say so out loud instead.
if python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        m = (json.load(f).get("mandate") or {})
    sys.exit(0 if m.get("active") else 1)
except Exception:
    sys.exit(1)' "$TARGET/.game_loop/state.json" 2>/dev/null; then
  echo
  echo "  ⚠ MIGRATION: .game_loop/state.json holds an ACTIVE mandate from before per-session state."
  echo "    State is now per Claude Code session (.game_loop/sessions/<id>/), so that mandate no"
  echo "    longer gates any session. In the session that owns that work, re-bind it:"
  echo "      ./.game_loop/bin/game_loop mandate --set \"<the mandate>\""
  echo "    then retire the old one:"
  echo "      GAME_LOOP_SESSION= ./.game_loop/bin/game_loop mandate --clear --notes \"migrated to per-session state\""
fi

echo
if [ "$FRESH" = 1 ]; then
  echo "Done — fresh install. Next:"
  echo "  1. Edit  $TARGET/.game_loop/INVARIANTS.md   — your project's north star"
  echo "  2. Edit  $TARGET/.game_loop/config.json     — read_roots / allow_write_roots / deploy_verbs"
  echo "  3. START A NEW CLAUDE CODE SESSION. Hooks are read at session start, so the hooks this"
  echo "     installer just wrote are NOT active in the session you ran it from — every gate is"
  echo "     registered on disk and silently never invoked. \`game_loop status\` will warn you."
  echo "  4. In that new session, run:  ./.game_loop/bin/game_loop status"
  echo "  5. To run unattended: ./.game_loop/bin/game_loop mandate --set \"<what to work on>\""
  echo "  6. OPTIONAL — Slack paging (arm questions, stuck runs, limit parks, phone replies):"
  echo "     create $TARGET/.game_loop/notify.json (gitignored; schema in .game_loop/bin/notify.py),"
  echo "     then verify with:  ./.game_loop/bin/game_loop notify --test"
else
  echo "Done — updated in place. Scripts refreshed; your config, invariants and notes were kept."
  echo "  Nothing else to do. Sanity-check with:  ./.game_loop/bin/game_loop status"
fi
