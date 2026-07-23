#!/usr/bin/env bash
# install bumper into a target project: copy the .bumper/ payload and wire the Claude Code hooks.
#
#   ./install.sh /path/to/your/project
#
# Idempotent: re-running updates the scripts and re-merges the hooks without duplicating them. Your
# state.json, config.json, INVARIANTS.md, verify.yaml and LEDGER.md are NEVER overwritten once they
# exist — those are yours. Only the bin/ scripts are always refreshed.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "usage: ./install.sh /path/to/your/project" >&2
  exit 1
fi
TARGET="$(cd "$TARGET" && pwd)"

if [ "$TARGET" = "$SRC" ]; then
  echo "That is the bumper_bot repo itself — it already dogfoods bumper. Point this at another project." >&2
  exit 1
fi

echo "Installing bumper into: $TARGET"
mkdir -p "$TARGET/.bumper/bin"

# Always refresh the executables — they are the tool.
cp "$SRC/.bumper/bin/bumper" "$SRC/.bumper/bin/watchdog" \
   "$SRC/.bumper/bin/guard-writes.sh" "$SRC/.bumper/bin/verify" "$TARGET/.bumper/bin/"
chmod +x "$TARGET/.bumper/bin/"*

# Seed the user-owned files only if absent — never clobber their config or notes.
seed() {
  if [ ! -e "$TARGET/.bumper/$1" ]; then
    cp "$SRC/.bumper/$1" "$TARGET/.bumper/$1"
    echo "  seeded  .bumper/$1"
  else
    echo "  kept    .bumper/$1 (already present)"
  fi
}
seed config.json
seed INVARIANTS.md
seed verify.yaml
seed LEDGER.md

# Set project_name in a freshly-seeded config to the target's directory name.
python3 - "$TARGET" <<'PY'
import json, os, sys
target = sys.argv[1]
cf = os.path.join(target, ".bumper", "config.json")
try:
    with open(cf) as f:
        c = json.load(f)
except (OSError, ValueError):
    sys.exit(0)
if c.get("project_name") in (None, "", "bumper_bot"):
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
GI="$TARGET/.bumper/.gitignore"
if [ ! -e "$GI" ]; then
  cat > "$GI" <<'EOF'
state.json
log.jsonl
verified.json
probe/
*.pid
.state.*.tmp
EOF
  echo "  wrote   .bumper/.gitignore"
fi

echo
echo "Done. Next:"
echo "  1. Edit  $TARGET/.bumper/INVARIANTS.md   — your project's north star"
echo "  2. Edit  $TARGET/.bumper/config.json     — read_roots / allow_write_roots / deploy_verbs"
echo "  3. In a Claude Code session in that repo, run:  ./.bumper/bin/bumper status"
echo "  4. To run unattended: ./.bumper/bin/bumper mandate --set \"<what to work on>\""
