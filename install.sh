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

# Always refresh the executables — they are the tool. (flair.py is decoration, imported by the others.)
cp "$SRC/.game_loop/bin/game_loop" "$SRC/.game_loop/bin/watchdog" \
   "$SRC/.game_loop/bin/guard-writes.sh" "$SRC/.game_loop/bin/verify" \
   "$SRC/.game_loop/bin/flair.py" "$TARGET/.game_loop/bin/"
chmod +x "$TARGET/.game_loop/bin/game_loop" "$TARGET/.game_loop/bin/watchdog" \
         "$TARGET/.game_loop/bin/guard-writes.sh" "$TARGET/.game_loop/bin/verify"
echo "  $([ "$FRESH" = 1 ] && echo copied || echo refreshed)  .game_loop/bin/ (game_loop, watchdog, guard-writes.sh, verify, flair.py)"

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
print("  merged  .claude/settings.json (PreToolUse guard + Stop gate + watchdog)")
PY

# Ignore the runtime files.
GI="$TARGET/.game_loop/.gitignore"
if [ ! -e "$GI" ]; then
  cat > "$GI" <<'EOF'
state.json
log.jsonl
verified.json
probe/
*.pid
.state.*.tmp
EOF
  echo "  wrote   .game_loop/.gitignore"
fi

echo
if [ "$FRESH" = 1 ]; then
  echo "Done — fresh install. Next:"
  echo "  1. Edit  $TARGET/.game_loop/INVARIANTS.md   — your project's north star"
  echo "  2. Edit  $TARGET/.game_loop/config.json     — read_roots / allow_write_roots / deploy_verbs"
  echo "  3. In a Claude Code session in that repo, run:  ./.game_loop/bin/game_loop status"
  echo "  4. To run unattended: ./.game_loop/bin/game_loop mandate --set \"<what to work on>\""
else
  echo "Done — updated in place. Scripts refreshed; your config, invariants and notes were kept."
  echo "  Nothing else to do. Sanity-check with:  ./.game_loop/bin/game_loop status"
fi
