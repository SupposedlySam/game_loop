---
name: gl-install
description: Install game_loop into a project, or check and refresh an install that is already there — deciding on its own whether this machine installs CENTRALLY (shared code, 5 shims per repo) or LOCALLY (a full copy per repo) by reading what is actually on disk, and asking only when the disk genuinely does not say. Use when the user says "install game_loop here", "set up game_loop", "gl-install", "add game_loop to this project", "upgrade game_loop", "is game_loop up to date", or runs /gl-install. For "what does game_loop want right now" use the game_loop skill instead; this one is the installer.
---

# gl-install

Get game_loop onto a project **without making the human answer a question the filesystem already
answers**. Two install shapes exist and they are not interchangeable:

| Shape | What lands in the repo | When it is right |
|---|---|---|
| **local** (default) | a full copy of the tool under `.game_loop/bin/` | one repo, or no shared install on this machine |
| **central** (`--central`) | 5 tiny dispatcher shims that run a shared, machine-wide copy | many repos, one place to upgrade |

Rules and config (`config.json`, `verify.yaml`, `INVARIANTS.md`, `LEDGER.md`) seed **locally either
way** — central shares the *code*, never the rules.

## Step 1 — Where is it going, and is it there already?

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && pwd
test -x .game_loop/bin/game_loop && echo "INSTALLED" || echo "ABSENT"
```

If `ABSENT`, go to Step 2. If `INSTALLED`, find out **which shape**, the same way the installer
itself does — a central install writes shims and no `guard-writes-impl.sh`:

```bash
test -f .game_loop/bin/guard-writes-impl.sh && echo "shape: LOCAL" || echo "shape: CENTRAL"
./.game_loop/bin/game_loop status | sed -n '1,3p;/PINNED CODE/,+2p'
```

Then go to Step 4 — an existing install is an upgrade decision, not a fresh one.

## Step 2 — Decide the shape by reading, not by asking

Walk these in order and **stop at the first one that answers**. Each rung is a real path on disk.

**Rung 1 — is this a linked worktree?** Then it is not a new project and it does not get its own
answer; the installer detects this itself and copies the main checkout's harness.

```bash
git rev-parse --git-common-dir     # differs from .git ⇒ this is a linked worktree
```

If it is, skip the shape question entirely and run the plain install (Step 3) — adoption is
automatic, and `--central` is not yours to choose for a tree that belongs to another checkout.

**Rung 2 — does a central install exist on this machine?**

```bash
GLC="${GAME_LOOP_CENTRAL:-$HOME/.claude/game_loop-central}"
test -x "$GLC/.game_loop/bin/game_loop" && echo "CENTRAL PRESENT: $GLC" || echo "no central install"
```

Present ⇒ **use `--central`.** That is the whole point of having set one up, and the shims cost this
repo nothing. Absent ⇒ Rung 3.

**Rung 3 — what do this machine's other game_loop repos do?** A convention already in use beats a
default. Cheap and bounded — look one level down from this repo's parent:

```bash
for d in ../*/.game_loop/bin/game_loop; do
  [ -e "$d" ] || continue
  r="${d%/.game_loop/bin/game_loop}"
  if [ -f "$r/.game_loop/bin/guard-writes-impl.sh" ]; then echo "LOCAL   $r"; else echo "CENTRAL $r"; fi
done
```

All agree ⇒ follow them. Nothing found ⇒ **local**, the default, no question asked.

## Step 3 — When to actually ask the human

Only these, and say what you found rather than offering a bare menu:

- **`GAME_LOOP_CENTRAL` is set but nothing is there.** Someone intended central and it is not
  populated. Do not silently fall back to local — that is how a machine ends up with a mix nobody
  chose. Offer: populate it, or install locally this once.
- **Rung 3 split** — neighbours disagree, some central, some local. Name the counts and ask which
  this repo joins.
- **No central install exists and the user has said they want many repos on this.** Then the
  one-time setup is the better answer than N local copies:

  ```bash
  ./.game_loop/bin/game_loop self --pin <ref> --dest ~/.claude/game_loop-central
  ```

  From any existing game_loop checkout. It is a deliberate, per-machine action — never do it
  unasked.

## Step 4 — Run it

Fresh, local:

```bash
curl -fsSL https://raw.githubusercontent.com/SupposedlySam/game_loop/main/install.sh | bash -s -- .
```

Fresh, central (Rung 2 said yes):

```bash
curl -fsSL https://raw.githubusercontent.com/SupposedlySam/game_loop/main/install.sh | bash -s -- --central .
```

From a local clone of game_loop, the same two with `./install.sh [--central] /path/to/project`.

**Upgrading an existing install** (Step 1 said `INSTALLED`): re-run the installer in the shape it
already has. It is idempotent — the `bin/` scripts refresh, and `config.json`, `INVARIANTS.md`,
`verify.yaml`, `LEDGER.md`, `state.json` are never overwritten. Passing `--central` to a repo that
is currently local converts it; **omitting** `--central` on a repo that is currently central
converts it back to full local copies. Neither is a no-op, so match the shape from Step 1 unless the
human asked to switch.

To upgrade the shared copy that every central repo runs, re-pin it once — the repos need no change:

```bash
./.game_loop/bin/game_loop self --pin <ref> --dest ~/.claude/game_loop-central
```

## Step 4b — The one question the installer remembers: the context cap

At the end of a run the installer asks whether to turn on the limit gate's **context trigger** — a
session past a token cap (default 300000) is refused ordinary tool calls until it hands off, because
a session re-sends its whole context on every call and a long run pays for its entire history every
turn. It is off unless somebody says yes, because it interrupts a run they are watching.

The answer is remembered for **15 days** in `~/.game_loop/install-answers.json`, so installing across
several repos is not the same question N times. Three things outrank the memory, in order: a flag,
an explicit `limits.context.enabled` already in the target's config, then the remembered answer.
Under all three, **no terminal means no** — which is what a piped `curl | bash` install gets, so
that path never enables it.

Answer for the human when they have already said what they want, rather than making them wait on a
prompt they cannot see:

```bash
./install.sh --context-cap /path/to/project            # on, at 300000
./install.sh --context-cap=200000 /path/to/project     # on, at a cap they named
./install.sh --no-context-cap /path/to/project         # off, and not remembered
```

A flag is that run's decision and is **not** cached — so `--no-context-cap` in CI never silences the
question a human would have been asked. The answer lands in `.game_loop/config.local.json`, the
gitignored layer: it is one person's preference, and the tracked `config.json` is the file every
fresh install copies from.

## Step 5 — Two refusals that are the installer working

- **`installed-by.json` present** — a package manager placed a blessed release here. Installing over
  it swaps a stamped release for whatever the source checkout is at this instant. The refusal names
  the packager's own upgrade command; use that. `--over-vendored` only if the human says so.
- **installed over a beta/stable install from an unmarked commit** — refused because it silently
  downgrades CONFIDENCE to alpha. Mark the source commit instead; `--over-blessed` is the override
  and it is the human's call, not yours.

Also expect `⚠ installed from an ALPHA commit` on a normal install. That is the **default**, not a
fault — nothing is marked unless someone marked it. For a marked one:

```bash
GAME_LOOP_CHANNEL=stable curl -fsSL https://raw.githubusercontent.com/SupposedlySam/game_loop/main/install.sh | bash -s -- .
```

## Step 6 — Say the one thing that is always true afterwards

**Hooks are read at session start.** Everything the installer just wrote is registered on disk and
invoked by nothing in the session it ran from. Tell the user to start a new Claude Code session (or
reload the window in the VSCode extension), then:

```bash
./.game_loop/bin/game_loop status
```

`status` says `⚠ HOOKS NOT LIVE` until that happens — treat that line as expected here, not as a
broken install.

## Never

- Never run the installer without telling the user first — it writes tracked files and merges hooks
  into `.claude/settings.json`.
- Never run `self --pin ... --dest ~/.claude/game_loop-central` unasked. It is machine-wide.
- Never assume a bare `game_loop` is on PATH. It is `./.game_loop/bin/game_loop`, project-local,
  unless the user added the README's shell function themselves.
- Never answer the shape question by preference. Rungs 1–3 are on disk; only a genuine tie or a
  contradiction earns an interruption.
