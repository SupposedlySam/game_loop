#!/usr/bin/env bash
# install game_loop into a target project: copy the .game_loop/ payload and wire the Claude Code hooks.
#
#   From a clone:   ./install.sh /path/to/your/project
#   One-liner:      curl -fsSL https://raw.githubusercontent.com/SupposedlySam/game_loop/main/install.sh | bash -s -- .
#   A worktree:     ./install.sh /path/to/project/.worktrees/feature      (adopts the project's rules)
#   A sibling tree: ./install.sh --same-as /path/to/project /path/to/copy
#
# The one-liner needs no local clone: the installer fetches the payload tarball from GitHub itself.
# Override the source with GAME_LOOP_REPO=owner/repo and GAME_LOOP_REF=branch|tag (default: main).
#
# Idempotent: re-running updates the scripts and re-merges the hooks without duplicating them. Your
# state.json, config.json, INVARIANTS.md, verify.yaml and LEDGER.md are NEVER overwritten once they
# exist — those are yours. Only the bin/ scripts are always refreshed.
#
# A LINKED WORKTREE is a second working copy of ONE project, not a new project (issue #30). Seeding
# the blank templates there hands it a DIFFERENT harness — most dangerously an empty verify.yaml,
# which owes nothing and so reports success while checking nothing. So a linked worktree is detected
# and the MAIN checkout's owned files are copied instead; if that checkout has no harness either,
# this refuses rather than quietly substituting defaults. Nobody has to know a flag for that.
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

usage() {
  cat <<'USAGE'
usage: ./install.sh [--same-as <checkout>] [--fresh] /path/to/your/project
       (or pipe via curl: ... | bash -s -- .)

  --same-as <checkout>  carry the SAME harness as <checkout>: copy ITS owned files rather than
                        seeding blank templates. A LINKED WORKTREE is detected and adopted with no
                        flag at all — reach for this only where git cannot connect the two trees
                        (a sibling clone, a copied directory, a tarball someone unpacked).
  --fresh               seed the blank templates even in a linked worktree whose main checkout has
                        no harness. The refusal's escape hatch: say it, and it is yours.
USAGE
}

TARGET=""
SAME_AS=""
FORCE_FRESH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --same-as)
      if [ $# -lt 2 ]; then
        echo "--same-as needs the path of the checkout whose harness this tree should carry." >&2
        exit 1
      fi
      SAME_AS="$2"; shift 2 ;;
    --same-as=*) SAME_AS="${1#--same-as=}"; shift ;;
    --fresh)     FORCE_FRESH=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    --)          shift ;;
    -*)          echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
    *)
      if [ -n "$TARGET" ]; then
        echo "install.sh takes ONE target directory (got '$TARGET' and '$1')." >&2
        exit 1
      fi
      TARGET="$1"; shift ;;
  esac
done

if [ -z "$TARGET" ]; then
  usage >&2
  exit 1
fi
if [ ! -d "$TARGET" ]; then
  echo "No such directory: $TARGET" >&2
  exit 1
fi
TARGET="$(cd "$TARGET" && pwd)"

if [ "$TARGET" = "$SRC" ]; then
  echo "That is the game_loop repo itself — it already dogfoods game_loop. Point this at another project." >&2
  exit 1
fi

# WHICH FILES ARE THE PROJECT'S is single-sourced in the payload — `game_loop owned --porcelain`
# (see OWNED_FILES in .game_loop/bin/game_loop). Read here rather than listed again, so adding an
# owned file is one line in one place instead of a hunt through the installer.
OWNED="$(python3 "$SRC/.game_loop/bin/game_loop" owned --porcelain 2>/dev/null \
         | python3 -c 'import json, sys
for o in json.load(sys.stdin)["owned"]:
    print(o["path"], o["seed_from"], "rule" if o["rule"] else "notes", sep="\t")' 2>/dev/null || true)"
if [ -z "$OWNED" ]; then
  echo "Could not read the owned-file set from the payload (game_loop owned --porcelain)." >&2
  echo "The payload at $SRC looks incomplete — re-fetch it." >&2
  exit 1
fi

# Does this checkout carry any of the project's own files? "Has a harness worth copying" — one
# owned file is enough, because a half-installed parent is still the project's rules and not ours.
has_owned() {
  local dir="$1" name rest
  while IFS=$'\t' read -r name rest; do
    [ -n "$name" ] || continue
    [ -e "$dir/.game_loop/$name" ] && return 0
  done <<EOF
$OWNED
EOF
  return 1
}

# The MAIN checkout of a linked worktree, or nothing. `git worktree list --porcelain` prints the
# main worktree FIRST; that is the whole detection. Every failure mode — no git, not a repo, a
# detached HEAD, a bare main worktree (no files to copy), a parent that has been deleted — leaves
# this printing nothing and returning non-zero, so the ordinary path is reached unchanged.
main_checkout_of() {
  local tree="$1" listing first path
  command -v git >/dev/null 2>&1 || return 1
  listing="$(git -C "$tree" worktree list --porcelain 2>/dev/null)" || return 1
  [ -n "$listing" ] || return 1
  first="$(printf '%s\n' "$listing" | awk 'NF==0 { exit } { print }')"
  if printf '%s\n' "$first" | grep -qx 'bare'; then return 1; fi
  path="$(printf '%s\n' "$first" | sed -n 's/^worktree //p' | head -1)"
  [ -n "$path" ] && [ -d "$path" ] || return 1
  path="$(cd "$path" 2>/dev/null && pwd -P)" || return 1
  [ "$path" != "$(cd "$tree" && pwd -P)" ] || return 1
  printf '%s\n' "$path"
}

# Resolve the adoption source BEFORE anything is copied: a refusal that has already written half a
# harness into the tree is not a refusal.
ADOPT_FROM=""
ADOPT_WHY=""
if [ -n "$SAME_AS" ]; then
  if [ "$FORCE_FRESH" = 1 ]; then
    echo "--same-as and --fresh ask for opposite things (adopt that tree's rules / seed blank ones)." >&2
    exit 1
  fi
  if [ ! -d "$SAME_AS" ]; then
    echo "--same-as: no such directory: $SAME_AS" >&2
    exit 1
  fi
  SAME_AS="$(cd "$SAME_AS" && pwd)"
  if [ "$SAME_AS" = "$TARGET" ]; then
    echo "--same-as points at the target itself — a tree already carries its own harness." >&2
    exit 1
  fi
  if ! has_owned "$SAME_AS"; then
    echo "REFUSED — --same-as $SAME_AS carries no game_loop files to copy." >&2
    echo "  Install there first, then run this again. Seeding the blank templates instead would" >&2
    echo "  hand this tree an empty verify.yaml, which owes nothing and reports success anyway." >&2
    exit 1
  fi
  ADOPT_FROM="$SAME_AS"
  ADOPT_WHY="--same-as"
elif PARENT="$(main_checkout_of "$TARGET")"; then
  if has_owned "$PARENT"; then
    ADOPT_FROM="$PARENT"
    ADOPT_WHY="linked worktree"
  elif [ "$FORCE_FRESH" = 1 ] || has_owned "$TARGET"; then
    : # an explicit --fresh, or a re-install in a tree that already has its own files
  else
    echo "REFUSED — $TARGET is a LINKED WORKTREE of" >&2
    echo "  $PARENT" >&2
    echo "and that checkout carries no game_loop files. These are two trees of ONE project, so" >&2
    echo "seeding the blank templates here would give this tree a DIFFERENT harness from the" >&2
    echo "project it belongs to — an empty verify.yaml owes nothing, so the commit gate would" >&2
    echo "report success while checking nothing at all." >&2
    echo >&2
    echo "  → install into the main checkout first:  ./install.sh $PARENT" >&2
    echo "  → or copy another tree's rules:          ./install.sh --same-as <checkout> $TARGET" >&2
    echo "  → or, if this tree really is its own project:  ./install.sh --fresh $TARGET" >&2
    exit 1
  fi
fi

# Fresh install vs update: decided before we copy anything, so the closing summary can say which.
if [ -f "$TARGET/.game_loop/bin/game_loop" ]; then FRESH=0; else FRESH=1; fi

echo "Installing game_loop into: $TARGET"
mkdir -p "$TARGET/.game_loop/bin"

# Always refresh the executables — they are the tool. (flair.py is decoration and notify.py is
# paging; both are imported by the others and both degrade to no-ops.)
cp "$SRC/.game_loop/bin/game_loop" "$SRC/.game_loop/bin/watchdog" \
   "$SRC/.game_loop/bin/guard-writes.sh" "$SRC/.game_loop/bin/guard-writes-impl.sh" \
   "$SRC/.game_loop/bin/guard-mcp.sh" "$SRC/.game_loop/bin/guard-mcp-impl.sh" \
   "$SRC/.game_loop/bin/verify" "$SRC/.game_loop/bin/flair.py" "$SRC/.game_loop/bin/notify.py" \
   "$TARGET/.game_loop/bin/"
chmod +x "$TARGET/.game_loop/bin/game_loop" "$TARGET/.game_loop/bin/watchdog" \
         "$TARGET/.game_loop/bin/guard-writes.sh" "$TARGET/.game_loop/bin/guard-writes-impl.sh" \
         "$TARGET/.game_loop/bin/guard-mcp.sh" "$TARGET/.game_loop/bin/guard-mcp-impl.sh" \
         "$TARGET/.game_loop/bin/verify"
echo "  $([ "$FRESH" = 1 ] && echo copied || echo refreshed)  .game_loop/bin/ (game_loop, watchdog, guard-writes.sh + -impl, guard-mcp.sh + -impl, verify, flair.py, notify.py)"

# Stamp the game_loop commit we installed from, so `status` can flag when a re-install is due. From a
# clone that's HEAD; from the curl/tarball path (no .git) ask GitHub for the ref's sha. Best effort.
GL_SHA=""
if git -C "$SRC" rev-parse HEAD >/dev/null 2>&1; then
  GL_SHA="$(git -C "$SRC" rev-parse HEAD)"
elif command -v curl >/dev/null 2>&1; then
  GL_SHA="$(curl -fsSL "https://api.github.com/repos/$REPO/commits/$REF" 2>/dev/null \
            | python3 -c 'import json,sys; print((json.load(sys.stdin) or {}).get("sha",""))' 2>/dev/null || true)"
fi
if [ -n "$GL_SHA" ]; then
  printf '%s\n' "$GL_SHA" > "$TARGET/.game_loop/VERSION"
  echo "  stamped .game_loop/VERSION ($(printf '%s' "$GL_SHA" | cut -c1-8)) — status flags when a re-install is due"
fi

# Seed the user-owned files only if absent — never clobber their config or notes. With an adoption
# source resolved, "absent" is filled from THAT tree instead of from the shipped templates: the two
# trees are one project, so its rules are the right default, and the blank template is the wrong one.
# $2 is where a FRESH seed comes from (templates/verify.yaml for the one that ships empty).
seed() {
  local dest="$1" src="${2:-.game_loop/$1}"
  if [ -e "$TARGET/.game_loop/$dest" ]; then
    echo "  kept    .game_loop/$dest (already present)"
  elif [ -n "$ADOPT_FROM" ] && [ -e "$ADOPT_FROM/.game_loop/$dest" ]; then
    cp "$ADOPT_FROM/.game_loop/$dest" "$TARGET/.game_loop/$dest"
    echo "  adopted .game_loop/$dest (from $ADOPT_FROM)"
  else
    cp "$SRC/$src" "$TARGET/.game_loop/$dest"
    echo "  seeded  .game_loop/$dest"
  fi
}
if [ -n "$ADOPT_FROM" ]; then
  echo "  adopting the harness of $ADOPT_FROM ($ADOPT_WHY) — its rules, not the blank templates"
fi
# Driven by the payload's own owned-file set (read into $OWNED above), so this loop never has to be
# edited when game_loop adds a file.
while IFS=$'\t' read -r OWNED_NAME OWNED_SRC OWNED_KIND; do
  [ -n "$OWNED_NAME" ] || continue
  seed "$OWNED_NAME" "$OWNED_SRC"
done <<EOF
$OWNED
EOF

# An adopted tree whose files were ALREADY there may still differ from the tree it belongs to — and
# `seed` correctly refuses to clobber them. Say so, loudly, rather than leaving two rule sets running.
if [ -n "$ADOPT_FROM" ]; then
  DRIFTED=""
  while IFS=$'\t' read -r OWNED_NAME OWNED_SRC OWNED_KIND; do
    [ -n "$OWNED_NAME" ] || continue
    [ "$OWNED_KIND" = "rule" ] || continue
    [ -e "$ADOPT_FROM/.game_loop/$OWNED_NAME" ] || continue
    cmp -s "$TARGET/.game_loop/$OWNED_NAME" "$ADOPT_FROM/.game_loop/$OWNED_NAME" \
      || DRIFTED="$DRIFTED $OWNED_NAME"
  done <<EOF
$OWNED
EOF
  if [ -n "$DRIFTED" ]; then
    echo "  ⚠ DRIFT — these were already present and DIFFER from $ADOPT_FROM, and were kept:"
    for D in $DRIFTED; do echo "      .game_loop/$D"; done
    echo "    Two trees of one project are enforcing two rule sets. Yours to resolve — nothing here"
    echo "    overwrites a file you own. \`game_loop status\` re-states it, and"
    echo "    \`game_loop worktree --porcelain\` answers it as JSON."
  fi
fi

# Set project_name in a freshly-seeded config to the target's directory name — but NEVER in an
# adopted one: there the name is the PROJECT's, and renaming it to this tree's directory name would
# manufacture the very drift the adoption just prevented.
RENAME_PROJECT=1
if [ -n "$ADOPT_FROM" ] && cmp -s "$TARGET/.game_loop/config.json" "$ADOPT_FROM/.game_loop/config.json"; then
  RENAME_PROJECT=0
fi
if [ "$RENAME_PROJECT" = 1 ]; then
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
fi

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
edited.txt
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
.update_cache.json
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
  if ! grep -q '^.update_cache.json$' "$GI"; then
    echo ".update_cache.json" >> "$GI"
    echo "  updated .game_loop/.gitignore (+ .update_cache.json — update-check cache)"
  fi
  # the no-session fallback's edited-path record (the per-session one is under sessions/)
  if ! grep -q '^edited.txt$' "$GI"; then
    echo "edited.txt" >> "$GI"
    echo "  updated .game_loop/.gitignore (+ edited.txt — what this session actually wrote)"
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
if [ "$FRESH" = 1 ] && [ -n "$ADOPT_FROM" ]; then
  echo "Done — this tree now carries the SAME harness as:"
  echo "  $ADOPT_FROM"
  echo "Its rules were copied, not re-seeded, so both trees enforce one project. Next:"
  echo "  1. START A NEW CLAUDE CODE SESSION — hooks are read at session start, so the ones this"
  echo "     installer just wrote are NOT active in the session you ran it from."
  echo "  2. In that new session, run:  ./.game_loop/bin/game_loop status"
  echo "     Its WORKTREE block re-checks this tree's rules against the project's every session."
  echo "  3. Provisioning trees from a script? \`game_loop worktree --porcelain\` is that check as"
  echo "     JSON (exit 0 only when it MATCHED), and \`game_loop owned --porcelain\` is the file set."
elif [ "$FRESH" = 1 ]; then
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
  echo
  echo "  TIP: game_loop is the project-local binary ./.game_loop/bin/game_loop — NOT a global command."
  echo "       For bare 'game_loop ...' from anywhere in the repo, add the shell function in the README."
else
  echo "Done — updated in place. Scripts refreshed; your config, invariants and notes were kept."
  echo "  Nothing else to do. Sanity-check with:  ./.game_loop/bin/game_loop status"
fi
