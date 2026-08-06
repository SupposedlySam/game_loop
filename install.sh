#!/usr/bin/env bash
# install game_loop into a target project: copy the .game_loop/ payload and wire the Claude Code hooks.
#
#   From a clone:   ./install.sh /path/to/your/project
#   One-liner:      curl -fsSL https://raw.githubusercontent.com/SupposedlySam/game_loop/main/install.sh | bash -s -- .
#   A worktree:     ./install.sh /path/to/project/.worktrees/feature      (adopts the project's rules)
#   A sibling tree: ./install.sh --same-as /path/to/project /path/to/copy
#
# The one-liner needs no local clone: the installer fetches the payload tarball from GitHub itself.
# Override the source with GAME_LOOP_REPO=owner/repo and GAME_LOOP_REF=branch|tag (default: main),
# or GAME_LOOP_CHANNEL=stable|beta to install the newest commit marked at that level.
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
LEVEL_REF=""     # set when REF itself names a confidence level — see the GL_LEVEL block below

# GAME_LOOP_CHANNEL=stable|beta — resolve "the newest commit we marked at that level" HERE, once,
# instead of in every consumer's script (#61).
#
# WHY THIS IS NOT MERELY A CONVENIENCE. The tags are ANNOTATED, so tag order is not commit order,
# and the two plausible sorts disagree:
#     git tag -l 'stable-*' --sort=-creatordate    -> newest      CORRECT
#     git tag -l 'stable-*' --sort=-committerdate  -> much older   WRONG
# Both look reasonable. A consumer who picks the wrong one silently pins an older commit, and the
# installer then correctly stamps THAT commit's level as stable — so the mistake is invisible
# downstream forever. A silent wrong answer is the failure this whole project is organised against,
# and leaving the knowledge in a README for everyone to re-implement is how it keeps happening.
#
# `git ls-remote --sort` needs no clone and asks the remote directly, so this costs one network call
# and cannot drift from what the repo actually carries.
if [ -n "${GAME_LOOP_CHANNEL:-}" ]; then
  case "$GAME_LOOP_CHANNEL" in
    stable|beta) : ;;
    *) echo "GAME_LOOP_CHANNEL must be 'stable' or 'beta' (got '$GAME_LOOP_CHANNEL')." >&2; exit 1 ;;
  esac
  if [ -n "${GAME_LOOP_REF:-}" ]; then
    echo "Set GAME_LOOP_CHANNEL or GAME_LOOP_REF, not both — two sources for one answer is the" >&2
    echo "shape where nobody can say afterwards which one was installed." >&2
    exit 1
  fi
  # The channel IS a ref, moved by whoever marked the commit. Nothing here sorts anything, needs
  # git, or reads a tag object: this resolves to one more codeload request on the path below.
  REF="$GAME_LOOP_CHANNEL"
fi

# Does the ref we are about to fetch NAME a confidence level? Both grains count: the moving channel
# pointer (`stable`) and one mark's immutable tag (`stable-<sha>`).
case "$REF" in
  stable|stable-*) LEVEL_REF="stable" ;;
  beta|beta-*)     LEVEL_REF="beta" ;;
esac

# Locate the payload. Running from a clone, it sits next to this script. Piped through `curl | bash`
# there is no checkout — fetch the repo tarball into a temp dir and use that. `--strip-components=1`
# drops the archive's top folder (game_loop-<ref>/) so we never have to guess its name.
#
# BASH_SOURCE[0] is the ONLY honest answer to "was I loaded from a file on disk?". A previous
# version fell back to `$0`, and that broke the documented one-liner on the one path that matters
# most: piped, `$0` is "bash", `dirname bash` is ".", and SRC silently became WHEREVER THE USER WAS
# STANDING. On a fresh directory that is harmless — the file test below fails and the fetch happens
# anyway — but on an UPGRADE the target already has .game_loop/bin/game_loop, so the test passed
# against the target's OWN (old) copy, the fetch was skipped, SRC and TARGET resolved to the same
# path, and the run died claiming you had pointed it at the game_loop repo. The single path that
# could never work was upgrading, which is exactly the path nothing else propagates either.
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
  SRC="$(cd "$(dirname "$SELF")" 2>/dev/null && pwd || true)"
else
  SRC=""   # piped: no local payload exists, and inferring one from the cwd is the bug above
fi
if [ -z "${SRC:-}" ] || [ ! -f "$SRC/.game_loop/bin/game_loop" ]; then
  command -v curl >/dev/null 2>&1 || { echo "curl is required to fetch game_loop." >&2; exit 1; }
  command -v tar  >/dev/null 2>&1 || { echo "tar is required to unpack game_loop." >&2; exit 1; }
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  echo "Fetching game_loop ($REPO@$REF) from GitHub…"
  mkdir -p "$TMP/payload"
  # A TAG IS A REF TOO (#61). This hardcoded refs/heads/, while the header has always documented
  # GAME_LOOP_REF as "branch|tag" — so the documented flag for the one thing a consumer most wants,
  # a marked release, 404'd. Measured: refs/heads/<tag> is 404, refs/tags/<tag> is 200. The branch
  # is tried first so the default path is unchanged and costs no extra request.
  if ! curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" 2>/dev/null \
       | tar -xz -C "$TMP/payload" --strip-components=1 2>/dev/null; then
    rm -rf "$TMP/payload"; mkdir -p "$TMP/payload"
    if ! curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/tags/$REF" 2>/dev/null \
         | tar -xz -C "$TMP/payload" --strip-components=1 2>/dev/null; then
      # SAY WHICH REF AND WHY, not `curl: (56) 404`. A channel that nobody has marked yet is the
      # likeliest way to land here, and it is a perfectly ordinary state — `beta` does not exist
      # until something is marked beta. An installer that answers that with a transport error makes
      # the reader debug their network instead of reading one sentence.
      echo "" >&2
      echo "No ref '$REF' in $REPO — tried refs/heads/$REF and refs/tags/$REF, both 404." >&2
      if [ -n "$LEVEL_REF" ] && [ "$REF" = "${GAME_LOOP_CHANNEL:-}" ]; then
        echo "" >&2
        echo "'$REF' is a CHANNEL POINTER, and it only exists once something has been marked at" >&2
        echo "that level. Nothing has been, or it was never pushed. Your options:" >&2
        echo "  * pick a level that exists:   git ls-remote --tags https://github.com/$REPO" >&2
        echo "  * install an exact release:   GAME_LOOP_REF=<tag> ..." >&2
        echo "  * install the tip:            omit GAME_LOOP_CHANNEL (records alpha, honestly)" >&2
      else
        echo "Check the spelling, or list what exists: git ls-remote https://github.com/$REPO" >&2
      fi
      exit 1
    fi
  fi
  SRC="$TMP/payload"
  FETCHED=1        # we downloaded $REF ourselves, so asking GitHub for its sha describes THIS payload
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
  # Name the EVIDENCE, not an inference about which repo you are in. The old wording asserted
  # "that is the game_loop repo itself", which cost a bug reporter a control run to disprove —
  # their directory was a stub, not this repo. What is actually known is that the two paths match.
  echo "REFUSED: the payload and the target are the same directory —" >&2
  echo "    $TARGET" >&2
  echo "Installing a checkout onto itself would copy bin/ over its own source. If this IS the" >&2
  echo "game_loop repo it already dogfoods game_loop and needs no install; if you meant to upgrade" >&2
  echo "a project, name that project's path instead of this one." >&2
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
   "$SRC/.game_loop/bin/limit-probe.sh" \
   "$TARGET/.game_loop/bin/"
chmod +x "$TARGET/.game_loop/bin/game_loop" "$TARGET/.game_loop/bin/watchdog" \
         "$TARGET/.game_loop/bin/guard-writes.sh" "$TARGET/.game_loop/bin/guard-writes-impl.sh" \
         "$TARGET/.game_loop/bin/guard-mcp.sh" "$TARGET/.game_loop/bin/guard-mcp-impl.sh" \
         "$TARGET/.game_loop/bin/verify" "$TARGET/.game_loop/bin/limit-probe.sh"
echo "  $([ "$FRESH" = 1 ] && echo copied || echo refreshed)  .game_loop/bin/ (game_loop, watchdog, guard-writes.sh + -impl, guard-mcp.sh + -impl, verify, flair.py, notify.py, limit-probe.sh)"

# The behaviour record ships and is ALWAYS refreshed — it is tool data, not one of the project's own
# files. Refreshing it is what makes the installed copy mean "the record as of your install", which
# is what `status` diffs against main to name changes you have not seen (#37). Seeding it once and
# leaving it would freeze the baseline at the first install and the notice would repeat forever.
if [ -f "$SRC/.game_loop/behaviour.json" ]; then
  cp "$SRC/.game_loop/behaviour.json" "$TARGET/.game_loop/behaviour.json"
  echo "  $([ "$FRESH" = 1 ] && echo copied || echo refreshed)  .game_loop/behaviour.json (what an existing verb now costs or refuses differently)"
fi

# THE AGENT BRIEF SHIPS (#60). It did not, and that was the hole under the whole of item 1: llms.txt
# is the file written for the agent, and it existed only in game_loop's own repo. An installed
# project had NOTHING for an agent to read — so a session that arrived by a global slash command,
# never having loaded the project's CLAUDE.md, met a refusal with no document behind it. A pointer
# is worthless without the thing it points at, and I nearly shipped one naming a path that is absent
# from every consumer. Refreshed like the executables, because it is tool documentation rather than
# one of the project's own files.
# The claims record ships and is always refreshed, like behaviour.json: it is tool data describing
# what this HARNESS believes about the host, not one of the project's own files. A consumer needs it
# because the belief is the harness's and the risk is theirs -- if the host moved, their limit gate
# is the thing that fails toward believing there is headroom.
if [ -f "$SRC/.game_loop/claims.json" ]; then
  cp "$SRC/.game_loop/claims.json" "$TARGET/.game_loop/claims.json"
  echo "  $([ "$FRESH" = 1 ] && echo copied || echo refreshed)  .game_loop/claims.json (what this harness believes about its host, and when it last checked)"
fi

if [ -f "$SRC/llms.txt" ]; then
  cp "$SRC/llms.txt" "$TARGET/.game_loop/llms.txt"
  echo "  $([ "$FRESH" = 1 ] && echo copied || echo refreshed)  .game_loop/llms.txt (the agent brief — what a refusal points at)"
fi

# Stamp the game_loop commit we installed from, so `status` can flag when a re-install is due. From a
# clone that's HEAD; from the curl/tarball path (no .git) ask GitHub for the ref's sha. Best effort.
GL_SHA=""
# WHOSE CHECKOUT IS THIS? git resolves by walking UP, so `git -C "$SRC" rev-parse HEAD` inside a
# VENDORED copy -- one with no .git of its own, which is what any extraction or package manager
# produces -- does not fail. It finds the CONSUMING repo's .git and answers with that repo's HEAD.
# Measured: a game_loop archive at .vendor/ inside a throwaway consumer reported the consumer's sha,
# not game_loop's, and would have stamped VERSION with a commit from an unrelated repository.
#
# The discriminator is the toplevel: a source that is its own checkout has one equal to itself.
# Nothing else distinguishes them, and the failure is silent in the worst way -- a plausible sha,
# from a real repo, that describes none of the installed files.
SRC_TOP="$(git -C "$SRC" rev-parse --show-toplevel 2>/dev/null || true)"
SRC_OWN=0
if [ -n "$SRC_TOP" ] && [ "$(cd "$SRC_TOP" 2>/dev/null && pwd -P)" = "$(cd "$SRC" 2>/dev/null && pwd -P)" ]; then
  SRC_OWN=1
fi
if [ "$SRC_OWN" = 1 ]; then
  GL_SHA="$(git -C "$SRC" rev-parse HEAD)"
elif [ -f "$SRC/.game_loop/VERSION" ]; then
  # A vendored tree may carry the stamp its packager wrote. That is the only party that knows which
  # commit the extraction came from, so it is the only honest source here.
  GL_SHA="$(tr -d '[:space:]' < "$SRC/.game_loop/VERSION")"
  echo "  note    the source is not its own git checkout; carried its recorded VERSION forward"
elif [ "${FETCHED:-0}" = 1 ] && command -v curl >/dev/null 2>&1; then
  # ONLY when we fetched it. Asking GitHub for $REF's sha describes the payload when the payload IS
  # that download -- and describes nothing when $SRC is a directory somebody handed us. A vendored
  # tree extracted from an OLD commit would otherwise be stamped with today's main, which is the
  # same lie as stamping the consumer's HEAD, arrived at by a different road.
  GL_SHA="$(curl -fsSL "https://api.github.com/repos/$REPO/commits/$REF" 2>/dev/null \
            | python3 -c 'import json,sys; print((json.load(sys.stdin) or {}).get("sha",""))' 2>/dev/null || true)"
fi
# THE STAMP MUST DESCRIBE WHAT LANDED. The payload is copied from the source WORKING TREE, while the
# sha comes from HEAD -- the same thing only when that tree is clean. Installing from a checkout with
# uncommitted work therefore stamped a commit whose content is not what was copied, and since
# `status` uses the stamp to decide whether a re-install is due, the target reported itself current
# while carrying files that exist in no commit. Reported by a downstream package manager that read
# install.sh rather than guessing, and verified here before fixing.
#
# NOT REFUSED, MARKED. Installing from a work-in-progress checkout is a legitimate thing to do -- it
# is how this repo is developed. What is not legitimate is recording it as though it were a commit.
# Only meaningful when the source IS its own checkout -- otherwise this compares against whichever
# repository git walked up into, which is the same defect one line down.
if [ "$SRC_OWN" = 1 ] && [ -n "$GL_SHA" ] && ! git -C "$SRC" diff --quiet HEAD 2>/dev/null; then
  GL_SHA="${GL_SHA}-dirty"
  echo "  ⚠ the source checkout has uncommitted changes, so what was copied is NOT $(printf '%s' "$GL_SHA" | cut -c1-8)."
  echo "    Stamped -dirty: the sha does not describe these files, and status will say so."
fi
if [ -z "$GL_SHA" ]; then
  echo "  ⚠ no VERSION stamped: the source is not its own git checkout and carries no recorded"
  echo "    VERSION, so nothing here knows which commit these files came from. Leaving it UNSET is"
  echo "    the honest answer — a wrong sha would make the update check confidently wrong."
fi
# WHAT LEVEL WAS THE SOURCE AT? A clone gives you whatever was on main that morning, so the sha
# alone does not say whether the author stands behind it. Carried into the target so `status` can
# say it later, when nobody remembers which commit they installed from.
# THE LEVEL RIDES TAGS, AND AN EXTRACTION HAS NO TAGS. A `git archive` of a marked commit carries
# none of them, so the resolver correctly finds no mark and correctly applies the default -- and a
# commit tagged stable installs itself as ALPHA. Measured end to end by a packager an hour after the
# scheme shipped, and it is the worse half of the same gap as the VERSION stamp: alpha is the
# DEFAULT, so the failure is silent by construction and reads as an honest answer.
#
# So the level travels as a FILE when there are no tags to read. Deliberately the SAME file this
# installer writes, rather than any packager's own format: a vendored tree that carries
# .game_loop/CONFIDENCE (and .game_loop/VERSION) is honoured, whoever produced it. game_loop names
# no package manager, and any of them can satisfy this with two lines and no coupling either way.
GL_LEVEL=""
if [ "$SRC_OWN" = 1 ]; then
  GL_LEVEL="alpha"
  for _t in $(git -C "$SRC" tag --points-at HEAD 2>/dev/null); do
    case "$_t" in
      stable-*) GL_LEVEL="stable" ;;
      beta-*)   [ "$GL_LEVEL" = "stable" ] || GL_LEVEL="beta" ;;
    esac
  done
elif [ "${FETCHED:-0}" = 1 ] && [ -n "$LEVEL_REF" ]; then
  # WE ASKED GITHUB FOR A LEVEL REF AND IT ANSWERED (#61). A codeload tarball carries no .git, so
  # `git tag --points-at` cannot run and the level fell through to the alpha DEFAULT — meaning an
  # install from a commit carrying a stable mark recorded ALPHA, and recorded it silently, because
  # alpha is what absence looks like. Correct by the scheme's rules and wrong about the world.
  #
  # Requesting refs/tags/stable-<sha> (or the moving channel ref) from the configured repo and
  # receiving a tree IS the proof: that resolution was done by the remote against its own refs, and
  # it is the same trust root as fetching the payload at all. What it does NOT prove is anything
  # about a repo nobody configured — GAME_LOOP_REPO points somewhere, and this records that
  # somewhere's judgement, exactly as the rest of the install does.
  GL_LEVEL="$LEVEL_REF"
  echo "  note    level taken from the ref this was fetched by ($REF) — a tarball carries no tags"
elif [ -f "$SRC/.game_loop/CONFIDENCE" ]; then
  GL_LEVEL="$(tr -d '[:space:]' < "$SRC/.game_loop/CONFIDENCE")"
  case "$GL_LEVEL" in
    alpha|beta|stable) echo "  note    the source is not its own checkout; carried its recorded level ($GL_LEVEL) forward" ;;
    *) GL_LEVEL="" ;;
  esac
fi
if [ -z "$GL_LEVEL" ]; then
  GL_LEVEL="alpha"
fi
printf '%s\n' "$GL_LEVEL" > "$TARGET/.game_loop/CONFIDENCE"
if [ "$GL_LEVEL" = "alpha" ]; then
  echo "  ⚠ installed from an ALPHA commit — nothing marks it, which is the DEFAULT rather than a"
  echo "    judgement. The author pushes while features are half-landed. For something they stand"
  echo "    behind: git tag -l 'beta-*' 'stable-*'"
else
  echo "  level   $GL_LEVEL — the source commit carries a $GL_LEVEL mark"
fi
if [ -n "$GL_SHA" ]; then
  printf '%s\n' "$GL_SHA" > "$TARGET/.game_loop/VERSION"
  echo "  stamped .game_loop/VERSION ($(printf '%s' "$GL_SHA" | cut -c1-8)) — status flags when a re-install is due"
  # #49: this install is the thing that INVALIDATES the update cache, so it must not leave it
  # behind. The cached `latest` was recorded before the sha just stamped existed, and comparing the
  # two guarantees a wrong answer for the whole TTL — precisely the window in which somebody runs
  # `status` to confirm the update worked.
  rm -f "$TARGET/.game_loop/.update_cache.json"
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
# Prefers a pinned checkout exactly as the hooks do, and — where it cannot find the tap at all —
# SAYS SO in the one place a statusline is guaranteed to be read. The old `else cat >/dev/null`
# exited 0, printed nothing and wrote nothing, so a mis-resolved path was indistinguishable from a
# healthy tap; the usage-limit gates it feeds all fail open, and would have done it in silence.
GL_STATUSLINE = ('p="${CLAUDE_PROJECT_DIR:-.}"; gl="$p/.game_loop_self/.game_loop/bin/game_loop"; '
                 '[ -x "$gl" ] || gl="$p/.game_loop/bin/game_loop"; '
                 'if [ -x "$gl" ]; then GAME_LOOP_HOME="$p/.game_loop" exec "$gl" statusline; '
                 'else cat >/dev/null; '
                 'echo "🎮 game_loop: statusline tap not found — usage-limit gates are inert"; fi')
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
write-guard-probe
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
triggers.json
triggers.d/
config.local.json
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
  # triggers.json is SITE wiring — local paths, room names, sometimes a credential in a command.
  # Ignored before anyone writes one, for the same reason notify.json is.
  if ! grep -q '^triggers.json$' "$GI"; then
    printf 'triggers.json\ntriggers.d/\nconfig.local.json\n' >> "$GI"
    echo "  updated .game_loop/.gitignore (+ triggers.json — this project's own triggers)"
  fi
  # the no-session fallback's edited-path record (the per-session one is under sessions/)
  if ! grep -q '^edited.txt$' "$GI"; then
    echo "edited.txt" >> "$GI"
    echo "  updated .game_loop/.gitignore (+ edited.txt — what this session actually wrote)"
  fi
  # the write guard's invocation mark, same fallback shape: runtime state, never committed.
  if ! grep -q '^write-guard-probe$' "$GI"; then
    echo "write-guard-probe" >> "$GI"
    echo "  updated .game_loop/.gitignore (+ write-guard-probe — the write guard's run mark)"
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
  echo "     Paths under your home go in as ~/... — the guard expands the tilde, and this file is"
  echo "     COMMITTED, so an absolute /home/you/... hands every clone a write root only you have."
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
