---
name: game_loop
description: Detect whether the current project has the game_loop Claude Code harness installed, and if so, run and interpret `game_loop status` (plus the composite health-check it implies) so a session knows exactly what game_loop wants right now instead of guessing. If not installed, offer to install it (plain or --central). Use when entering any project directory and unsure whether game_loop guards it, or on "check game_loop", "run game_loop status", "game_loop doctor", "is game_loop set up here", "set up game_loop", "install game_loop" — and when a game_loop-style write/claim/commit refusal shows up and its meaning needs decoding.
---

# game_loop

game_loop is a Claude Code harness a project installs into itself (`.game_loop/`) to get a write
guard, a claim gate, a commit/verify gate, a watchdog, and usage-limit survival. It is **project-local**
(`./.game_loop/bin/game_loop`), never a global command. This skill removes the guesswork of "does this
project have it, and what does it want right now" — deterministically, from real command output only.

There is **no `game_loop doctor` subcommand** (verified against the CLI's argparse subcommands: `status,
claim, note, harden, stepback, trans, authorize, attribute, mandate, arm, checkpoint, pin, effector,
instrument, fix, measure, notify, owned, worktree, self, limitprobe, confidence`, plus hook-only
entrypoints `stopgate/limitgate/statusline/sessionstart`). Never invent one. "Doctor" below is a
composition of `status` (always) plus `confidence` and `worktree` (situationally) — nothing else.

## Step 1 — Detect whether this project has it

Walk **up** from the current directory looking for an executable `.game_loop/bin/game_loop` — the same
way the optional shell function in game_loop's own README does it, so a subdirectory or a linked git
worktree still finds the nearest harness:

```bash
d="$PWD"
while [ "$d" != "/" ]; do
  if [ -x "$d/.game_loop/bin/game_loop" ]; then
    echo "FOUND: $d/.game_loop/bin/game_loop"
    break
  fi
  d="$(dirname "$d")"
done
```

If the loop finishes with nothing printed, this project has no game_loop harness. Go to Step 2.
Otherwise `$d` is the project root every command below runs from.

## Step 2 — Not installed: offer, don't just do

State plainly that this project isn't guarded by game_loop, then offer — do not run the installer
without the user's go-ahead, since it edits `.claude/settings.json` and adds tracked files:

```bash
curl -fsSL https://raw.githubusercontent.com/SupposedlySam/game_loop/main/install.sh | bash -s -- .
```

Check for a machine-wide central install before defaulting to the plain form:

```bash
test -x "${GAME_LOOP_CENTRAL:-$HOME/.claude/game_loop-central}/bin/game_loop" && echo "central install present"
```

If that exists, the lighter option is available (writes 5 tiny dispatcher shims instead of a full
copy) — same one-liner, with the flag threaded through the pipe:

```bash
curl -fsSL https://raw.githubusercontent.com/SupposedlySam/game_loop/main/install.sh | bash -s -- --central .
```

If no central install exists yet and the user wants one across many repos, that's a one-time,
per-machine setup (`game_loop self --pin <ref> --dest ~/.claude/game_loop-central`, from any existing
game_loop checkout) — point at the "Central install" section of that project's `docs/how-it-works.md`
rather than trying to script it here; it's a deliberate, occasional action, not a per-project default.

After install: hooks are read at session start, so tell the user to **restart Claude Code** (or, in the
VSCode extension, reload the window — it registers hooks at window load) before continuing, then go to
Step 3.

## Step 3 — Installed: run status and read it faithfully

```bash
cd "$d" && ./.game_loop/bin/game_loop status
```

`status` is the rehydration point after every compaction — read what it actually prints, don't
summarize from memory of a prior run. Known blocks, top to bottom, and what each means:

| Block / line | Meaning |
|---|---|
| `=== game_loop (vX) ===` + session line | which install, which session's state file this is |
| `claims sourced: N · hardened: N` | epistemic-gate usage counters |
| `MANDATE: ...` | **none (Stop gate inert)** = no autonomy job bound, ordinary conversation; **bound text** = an autonomy job is open, Stop gate is live; **⏸ PARKED** = open work a human interrupted — not done, resume with `mandate --resume` |
| `COST LADDER` | the cheap-rung-first menu of `claim` / `pin` / `effector` / `fix` / `instrument` |
| `PINNED CODE` (if present) | this checkout is running code from a pinned/central location, not its own `.game_loop/bin` |
| pins / effectors / instruments / fixes reports | load-bearing env facts, proved effectors, admitted metrics, proved fixes still on record this session |
| `COVERAGE — what these rails are NOT checking` | how many verify.yaml rules exist, how many changed paths are UNCHECKED |
| `WORKTREE` (only in a linked worktree) | **RULES DIFFER** = this tree enforces different rules than the main checkout (fix: `install.sh --same-as <main> <this>`); **RULES MATCH** = fine |
| `RULED OUT (N)` | claims already refuted with evidence — don't re-walk these |
| denials / triggers / working-tree / waiting reports | the harness actually refusing a tool call; trigger last-run status; which tree is being edited |
| `limits: ...` | usage-limit snapshot; `⚠ HANDOFF DUE` means the limitgate is currently closed |
| `⚠ USAGE-LIMIT PROTECTION IS INERT` | no statusline snapshot — only matters if you're relying on usage-limit survival |
| `notify: ...` | Slack paging configured or not |
| `⚠ HOOKS NOT LIVE` | **the most important one** — no record of the Stop gate ever firing in this checkout; hooks are read at session start, so a mid-session install leaves gates silently unwired. Fix: new session (or VSCode window reload) |
| `confidence: installed from a BETA/STABLE commit` or `⚠ INSTALLED FROM AN ALPHA COMMIT` | how much this specific commit is stood behind |
| `config: N key(s) overridden by config.local.json` | informational, site-local config layering |
| update notice | a newer game_loop exists on `main` (or a packager's own upgrade command, if `installed-by.json` names one — printed, never run automatically) |
| 🎮 flair line | cosmetic; ignore |

## Step 4 — "Run doctor" = triage status, plus two situational verbs

There is no dedicated command. Treat any of these lines from `status` as needing attention:

- `⚠ HOOKS NOT LIVE` — gates aren't enforced this session yet.
- `⚠ USAGE-LIMIT PROTECTION IS INERT` — only matters for unattended/long runs.
- `COVERAGE` showing `UNREADABLE`, or a nonzero `UNCHECKED` count on changed paths.
- `WORKTREE` showing `RULES DIFFER`.
- Any trigger reported `FAILING`.
- `⚠ INSTALLED FROM AN ALPHA COMMIT` — the default; not automatically bad, but worth surfacing.
- An update notice naming a newer `main`.

Then, for the commit-level trust question status only summarizes:

```bash
./.game_loop/bin/game_loop confidence
```

Reports alpha/beta/stable **and the evidence** for this exact commit (`git tag -l 'beta-*' 'stable-*'`
names what upstream stands behind).

If Step 3's `status` printed a `WORKTREE` block, get the scriptable verdict:

```bash
./.game_loop/bin/game_loop worktree --porcelain
```

Exit 0 only when clean; 1 = rules drifted; 3 = notes drifted; 2 = could not determine (never read a 2
as clean).

## Step 5 — "What does it want me to do right now"

Read straight off the `MANDATE:` line from Step 3:

- **Bound, not parked** — an autonomy job is open and the Stop gate is live. Look for what's
  outstanding (the mandate text + any prior checkpoint notes). Report progress with
  `game_loop checkpoint --notes ".."`, or close it with `game_loop mandate --clear --notes ".."` once
  genuinely done.
- **⏸ PARKED** — a human called a break mid-job. Resume with `game_loop mandate --resume` before
  picking the work back up.
- **none (Stop gate inert)** — nothing is bound; this is an ordinary session. The write guard, claim
  gate, and MCP guard are still always live regardless of mandate state.
- A non-empty **RULED OUT** list means those specific claims were already probed and killed — don't
  re-derive them.

`status` and `mandate` are the two verbs a human types; everything else in the cost ladder is the
agent's own, documented for it in that project's `llms.txt`.

## Never

- Never run the installer, plain or `--central`, without telling the user first and getting a
  go-ahead — it writes tracked files and merges hooks into `.claude/settings.json`.
- Never invent a `game_loop doctor` subcommand, or pass `--doctor`/`--health` to any verb. It doesn't
  exist; verified against the CLI's own argparse definitions.
- Never assume a bare `game_loop` command is on PATH — it's project-local unless the user has added
  the optional shell function from that project's README themselves.
- Don't confuse a project's own `.claude/skills/<something>/SKILL.md` (per-project, installed by some
  *other* tool's own installer) with this skill — this one is user-level and applies everywhere.
