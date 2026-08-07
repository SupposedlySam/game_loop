# game_loop

**Your AI coding agent can work while you're not watching. This is what makes that safe.**

You already know it can write code. The question this answers is different: *can you walk away?*

---

## What actually happens when you walk away

Not hypotheticals. These are the failures that produced every guard in this repo — each one observed
in a real run, usually while somebody was asleep or in a meeting.

**It stops.** You come back after an hour and it has been idle for fifty-five minutes, waiting for a
"continue" nobody typed. The work it promised is exactly where you left it.

**It says it's finished when it isn't.** Tests pass — because they tested nothing. A bug is "fixed"
because the thing that used to crash no longer crashes, which is not the same as the feature working.

**It states things that are confidently, plausibly false.** *"The library handles retries
internally."* It doesn't. Three files are now built on that sentence, and it reads exactly like the
sentences that were true.

**It touches things it shouldn't.** An agent running unattended with shell access, tidying up, is a
sentence that should worry you. Most of the time it's fine. You don't get to see the times it isn't
until afterwards.

**It burns your usage window and dies mid-thought.** No handoff, no notes, nothing written down. The
next session starts from nothing and re-derives what the last one already knew.

**It forgets what you told it.** Long sessions get compacted. The rule you gave it two hours ago is
gone — and it has no way of knowing it's gone.

**It commits more than it changed.** A formatter ran across a directory, `git add -A` swept it up, and
the commit message describes something else entirely.

---

## What game_loop does about each

| When this happens | What stops it |
|---|---|
| It stops early | A watchdog notices the session went quiet while work is outstanding, and starts it again. |
| It quits mid-task | A gate refuses the turn-end unless it reports progress, asks a real question, or the work is genuinely done. |
| It asks you something it could look up | A question costs it something: it must name the file it already read that failed to answer. |
| It asserts something false | It cannot claim anything about the outside world without naming a real file it read. Prose cannot satisfy that check. |
| It says "fixed" too early | A fix must be proved by exercising what the fix *produces* — re-running the thing that used to break does not count. |
| It edits outside the project | Everything outside the repo is read-only. The exception is one-time, spelled out by you, and logged. |
| It hits your usage limit | It is required to write a handoff before the window closes, then parks and wakes itself when the limit resets. |
| It forgets a rule | Rules become artifacts — a check, a test, a gate — rather than something it has to remember. |
| It commits work nobody looked at | The commit names files this session never touched, so a widened diff is visible before it lands. |

None of this is advice given to the model. Every one is a **hook** — code that runs whether or not the
agent cooperates, agrees, or remembers.

> **The rule everything follows from:** if the agent ignored every instruction you gave it, would this
> still hold? If no, it isn't a guardrail — it's a wish. Long sessions break wishes.

---

## Why you'd want this

**You get the hours back.** The realistic alternative to an unattended agent isn't a faster agent —
it's you, checking on it. game_loop is what makes *"go do this, I'll read it later"* a reasonable
thing to say.

**You can trust the report.** The expensive failure isn't an agent that gets stuck; it's one that
tells you it succeeded. Most of these guards exist to make "done" mean something.

**It gets stricter as it learns.** When something goes wrong, the fix isn't a note in a document
nobody re-reads — it becomes a check that fails next time. The harness gets harder to fool the longer
you run it.

**It's small, and it's yours.** Python standard library and bash. No services, no accounts, no
telemetry, nothing phoning home. You can read the whole thing in an afternoon.

## What it does not do

Stated plainly, because a tool that oversells its guarantees is worse than one that makes none.

- **It is not a sandbox.** It reduces blast radius; it does not contain a determined process. Run
  genuinely untrusted work in a VM.
- **It cannot see everything.** A mutation made through an interpreter one-liner, or through a path
  built from a shell variable, is outside what the write guard reads. It says so in its own output
  rather than implying coverage it does not have.
- **It does not make the agent smarter.** It makes it honest and persistent. A confused agent guarded
  by game_loop is a confused agent that has stopped claiming to be finished.
- **Some parts need a terminal.** Usage-limit survival reads data Claude Code exposes only to a
  terminal status line. In editor-embedded sessions it says so out loud rather than pretending to
  protect you.

---

## Requirements

- **Python 3** — standard library only, nothing to install
- **[Claude Code](https://claude.com/claude-code)** — the guards are Claude Code hooks
- macOS or Linux (`bash` + POSIX tools)

## Install

One line, in the project you want guarded — no clone needed:

```bash
curl -fsSL https://raw.githubusercontent.com/SupposedlySam/game_loop/main/install.sh | bash -s -- .
```

Then **restart Claude Code** (hooks are read when a session starts) and confirm:

```bash
./.game_loop/bin/game_loop status
```

Installing adds a `.game_loop/` directory and merges its hooks into `.claude/settings.json`. It never
overwrites files you own.

Installing several repos on one machine? `install.sh --central` wires a repo to run the tool from one
shared, machine-wide location instead of copying it in — see "Central install" in
[docs/how-it-works.md](docs/how-it-works.md) for setup and tradeoffs.

## Your first unattended run

Three steps. This is genuinely most of what a human does.

```bash
# 1. Give it a job it isn't allowed to abandon
./.game_loop/bin/game_loop mandate --set "get the integration tests passing"

# 2. Walk away. The agent works; the gates keep it moving and honest.

# 3. When you're back
./.game_loop/bin/game_loop status
```

While a mandate is bound the agent cannot end its turn by drifting off — it has to report progress,
ask you something it has earned the right to ask, or finish. Release it when the work is done:

```bash
./.game_loop/bin/game_loop mandate --clear --notes "tests green, flaky one quarantined"
```

Called away mid-run? `mandate --park --reason "..."` pauses without pretending the work is finished.

With **no mandate bound every gate is inert** — game_loop never sits between you and an ordinary
conversation. A mandate also binds only the session that set it, so another window on the same repo is
never conscripted into this one's work.

> 🤖 **Pointing an agent at this repo?** Send it to **[`llms.txt`](llms.txt)** — the operational brief:
> the exact commands, the gates it will hit, and what each one wants. This README is for you; that
> file is for it.

---

## Page your phone, not your terminal (optional)

If the agent genuinely needs you, it can reach you on Slack rather than blocking on a terminal nobody
is looking at — and if you reply from your phone, your answer is carried back into the run and it
keeps going. Configure in `.game_loop/notify.json`; see [`bin/notify.py`](.game_loop/bin/notify.py).

## Surviving usage limits

As a usage window approaches its limit, the agent is required to write a handoff before doing anything
else, so the run ends with its state on disk instead of mid-sentence. When the window is exhausted the
watchdog parks rather than burning retries against a wall, and wakes the session when the limit resets.

**The honest caveat:** Claude Code exposes usage data to a terminal status line and nowhere else. In a
session that renders no status line these gates cannot arm — and `status` tells you so in plain terms
rather than staying quiet and letting you assume you are covered.

**If your sessions run in an editor**, the snapshot those gates read comes only from a terminal
status line — so an editor-embedded session produces none, and `status` says the protection is
INERT rather than implying cover it does not have.

It can still *consume* one. The snapshot is a file in the checkout (`.game_loop/limits.json`) and
the limits are account-wide, so **one terminal session in that repo arms the gate for every other
session in it**, editor-hosted included. Keep an ordinary `claude` session open in a terminal on
the same repo — doing real work, not polling — and the rest inherit its readings. Nothing to
configure, and no probe that spends API turns to measure how many API turns you have left.

The honest limit: a window whose reset time has passed no longer binds, so a snapshot from last
week protects nothing. This helps while a terminal session is running or has run recently, and
`status` tells you which of those is true.

**Or let it fetch its own reading.** If keeping a terminal open is not practical, game_loop can
spawn a short session purely to read the windows and write the snapshot:

```json
"limits": { "probe": { "enabled": true, "min_interval_sec": 900, "max_interval_sec": 3600 } }
```

**Off by default, and it is not free**: a spawn costs about 24k input tokens (measured, and it is
the host's floor rather than ours). The reason to pay it is that the alternative is not zero — an
unattended run that hits its limit at 1am dies mid-action and is still dead at 7am, because nothing
could see the wall coming. The whole limit family already works; it was only ever missing the
snapshot.

The watchdog refreshes it on an interval **the reading itself implies** — the fullest window
decides, a window about to reset is not urgent however full it is, and with no snapshot at all it
waits the longest, since the first probe has the least information and should not also be the most
frequent. Run one by hand with `game_loop limitprobe --force`.

## Optional: let bare `game_loop` work anywhere in the repo

game_loop is a **project-local binary** (`./.game_loop/bin/game_loop`), never a global command — one
machine can hold several projects on different versions, and a global `game_loop` would run the wrong
one. If you want to type `game_loop ...` from anywhere inside a guarded repo, add this to your shell
profile:

```bash
game_loop() {
  local d="$PWD"
  while [ "$d" != "/" ]; do
    if [ -x "$d/.game_loop/bin/game_loop" ]; then
      "$d/.game_loop/bin/game_loop" "$@"
      return $?
    fi
    d="$(dirname "$d")"
  done
  {
    printf 'game_loop: no .game_loop/bin/game_loop found from %s upward.\n' "$PWD"
    printf '  This is a project-local harness, not a global command. cd into a guarded repo.\n'
  } >&2
  return 127
}
```

It walks **up** from wherever you are rather than assuming a repo root, which is what keeps it correct
in a subdirectory and inside a linked git worktree — each tree carries its own harness, and a function
hardcoded to one checkout would silently run a different tree's binary.

It finds the **nearest** harness walking up, which is what you want — and does mean a stray
`.game_loop/` in a parent directory wins over nothing at all. (Found while testing this: an old
harness left in `/tmp` made every path under `/tmp` resolve to it. Harmless, but surprising if you
have forgotten it is there.)

## Optional: a Claude Code skill so a session never has to guess

`templates/skills/game_loop/SKILL.md` is a **user-level** skill (not installed by `install.sh` —
that only ever writes inside the project it targets, never to your personal `~/.claude/`). Copy it
once to make it available in every project you work in:

```bash
mkdir -p ~/.claude/skills/game_loop
cp templates/skills/game_loop/SKILL.md ~/.claude/skills/game_loop/SKILL.md
```

It teaches a session to detect whether the current project has game_loop installed, offer to install
it (plain or `--central`) if not, and otherwise run and correctly interpret `game_loop status` —
including composing a "doctor"-style health check out of real verbs (`status`, `confidence`,
`worktree --porcelain`) rather than guessing at one that doesn't exist.

A shell function cannot reach non-interactive shells that skip your profile, so this is a convenience
for you and never something the tool relies on. Everything game_loop prints names the explicit path
for exactly that reason.

## How much do we stand behind a given commit?

game_loop is developed in the open, so `main` moves while features are half-landed. A clone gives you
whatever was there that morning. Three levels say how much confidence a specific commit has earned —
and each one is an **artifact**, not a claim we typed:

| level | what it means | what makes it markable |
|---|---|---|
| **alpha** | the default. Nothing marks this commit. Treat it as mid-flight. | nothing — it is the *absence* of a mark, so silence can never read as confidence |
| **beta** | the full suite passed on this exact tree | a clean tree and every owed check already run; refused otherwise |
| **stable** | our own agent was running its harness on this commit | the above, plus the pin equalling it — dogfooding as a fact, not a promise |

```bash
./.game_loop/bin/game_loop confidence      # what is this commit, and on what evidence
git tag -l 'beta-*' 'stable-*'             # commits we stand behind
```

Levels ride annotated **git tags**, so they arrive with an ordinary `git clone` and the evidence
travels in the tag message. `install.sh` records the level it installed from, and `status` says so
later, when nobody remembers which commit they took.

### Installing the latest stable

```bash
export GAME_LOOP_CHANNEL=stable
curl -fsSL https://raw.githubusercontent.com/SupposedlySam/game_loop/main/install.sh | bash -s -- .
```

The `export` is on its own line on purpose. In `VAR=x curl … | bash`, the assignment applies to
**`curl` only** — the `bash` that actually runs the installer never sees it, so you silently get an
`alpha` install of `main`. Nothing can warn you, because the installer never received the variable
and cannot tell that run from an ordinary one. (`curl … | GAME_LOOP_CHANNEL=stable bash -s -- .`
works too, and survives reformatting less well.)

`stable` and `beta` are **moving pointers**, re-aimed at each mark by whoever marks it, so nothing on
your side has to work out which tag is newest. That matters more than it sounds: the marks are
*annotated* tags, so tag order is not commit order — `--sort=-creatordate` is right and
`--sort=-committerdate` returns something much older, both look reasonable, and picking wrong pins an
older commit that `install.sh` then correctly stamps as stable. Nothing downstream ever contradicts
it. Sorting them yourself is the one step here with a silent wrong answer in it, so it happens once,
at the source.

Pin an exact release instead with `GAME_LOOP_REF=stable-<sha>` — immutable, where the channel moves.
Either way the level is recorded from the ref that was fetched, so a tarball with no `.git` no longer
falls through to `alpha`.

**Vendoring game_loop into another project?** An extracted copy has no git tags, so the level cannot
be read from it — and because `alpha` is the default, the failure would be silent and would look like
an honest answer. Carry two files into the extraction and `install.sh` honours them:

```
.game_loop/VERSION       the sha the extraction came from
.game_loop/CONFIDENCE    alpha | beta | stable
```

That is the whole contract. game_loop names no package manager and needs nothing from one at runtime;
whoever produces the tree is the only party that knows which commit it holds, so they are the only
party that can honestly say. Read the tags **live** when you write it rather than snapshotting at
publish time: a commit is usually marked *after* it exists, so a snapshot would record it unmarked
forever.

**What no level means:** none of them say the code is correct — only what was *checked*, and by whom.
`beta` says a suite passed. `stable` says we were running on it. Neither is a promise about your
project, and `confidence` tells you how to re-check rather than trust the tag.

## The guardrails, briefly

Full design in **[docs/how-it-works.md](docs/how-it-works.md)**; the guarantees as runnable checks in
**[`test/run.py`](test/run.py)**.

- **Claim gate** — no assertion about a dependency, a harness, or another repo without naming the real
  file that backs it.
- **Write guard** — an allowlist: the repo, the OS temp dir, and roots you configure. Everything else
  is read-only, including sibling projects. The escape hatch is you — single-use, and logged.
- **MCP guard** — a connected MCP server can delete or force-push with no shell command at all. Calls
  are classified before they run, and anything unclassifiable is refused. You can shut the plane off
  (`mcp_writes: "disabled"`) or pre-authorise a narrow set (`mcp_standing_writes`); see Configure.
- **Commit blast radius** — names the staged files this session never wrote, so a widened commit is
  visible before it lands.
- **verify** — your own map from "you changed X" to "these checks must pass". Refuses a commit when the
  evidence is older than the change. Ships empty; it does nothing until you add rules.

## The verbs

`status` and `mandate` are the two you will type. The rest are the agent's, and every one is
documented for it in [`llms.txt`](llms.txt). The short version:

| Command | What it's for |
|---|---|
| `status` | Rehydrate after compaction. The agent runs this first, every session. |
| `mandate --set` / `--clear` / `--park` | Bind, release, or pause a job it cannot abandon. `--park` is you calling a break. |
| `checkpoint --notes ".."` | Report progress and hand back without asking anything. |
| `arm --question .. --read .. --predict ..` | Spend one interruption of you, backed by a file it already read. |
| `claim --assert ".." --read <path>` | Assert something about the outside world, with the receipt. |
| `harden --learning .. --artifact <path>` | Turn a lesson into something enforced instead of remembered. |
| `authorize --path <prefix> --reason ".." [--uses N]` | Your one-time, logged permission for a single write outside the repo. `--uses N` when you authorised a run of several, rather than being interrupted once per call. |
| `confidence --mark beta\|stable [--ref <sha>] [--recheck]` | Record how much this project stands behind a commit. `--recheck` re-runs the gate instead of trusting the tag. |
| `<any verb> --<option>-file <path>` | Read a prose option from a file. Required over 400 characters — a shell mangles prose that quotes code. |

## Configure

Everything lives in `.game_loop/config.json`. It ships with sane defaults and comments; see
[docs/how-it-works.md](docs/how-it-works.md) for what each knob changes.

| Key | What it does |
|---|---|
| `read_roots` | Extra directories a `claim --read` may cite, beyond this repo. |
| `allow_write_roots` | Extra roots the write guard permits. **An absolute home path here ships a write permission to everyone who clones you.** |
| `deploy_verbs` | Extra deploy/publish commands to block outright. This rail is a *denylist* — a verb nobody listed is not blocked. |
| `mcp_writes` | `"gated"` (default: a mutating MCP call is refused, and you may open it once with `authorize`) or `"disabled"` (refused outright, no hatch offered). |
| `mcp_trusted_servers` | Whole servers your project OWNS. Every call to them is allowed, destructive included — the widest door here. See below. |
| `mcp_standing_writes` | A narrow set of MCP writes that need no human, in either of two grains — an exact `mcp__server__tool`, or a whole-server `mcp__server__` prefix. See below. |
| `mcp_read_only_tools` | Teaches the MCP guard which *ambiguous* tools of a server you trust are read-only. It can only resolve ambiguity — never silence a mutating verb or a mutating argument. |
| `watchdog` | Ring timing, ring cap, and `waiting_probe`. |
| `limits` | Usage-limit thresholds for the handoff gate and the park. |
| `update_check`, `update_repo` | Whether to check for a newer game_loop, and where from. `update_api_base` / `update_raw_base` retarget those lookups (GitHub Enterprise, a mirror, or a test). |
| *(not config)* `.game_loop/installed-by.json` | Written by a **packager**, not by you — see below. |
| `session_start` | `false` disables the status block injected at session start and after compaction. |
| `work_nudge_every`, `trans_nudge_every` | How much evidence work goes by before `status` nudges for a retro / a phase transition. |
| `hooks_probe_slack_sec` | How stale a hook probe may be before `status` calls the wiring into question. |
| `project_name`, `flair` | Display name, and the fun lines (`flair.enabled: false` to opt out). |

**Standing MCP writes.** Ask-every-time is the wrong shape when a workflow's *work product* lands
through an MCP write — a finished review that cannot be posted buys a retry, not a safety decision.

```json
"mcp_standing_writes": ["mcp__github__", "mcp__other__createThing"]
```

A **whole-server prefix** is the right grain when the server is your own first-party code, because
enumerating its tools goes stale every time it grows one — and it goes stale *toward a stuck agent*.
It is safe because every floor runs on the **live call** and returns before this policy is consulted:
a prefix widens *which servers* are trusted, never *what* may be done through them. One tier a prefix
does **not** inherit — `merge`, `publish`, `deploy`, `release`, `push` still need the tool named
exactly. Typing it out is the deliberate act; inheriting it from which list a verb sits in is not.

**When the server is yours.** `mcp_standing_writes` deliberately stops short of the irreversible
and landing tiers — right for a server somebody else ships, wrong for one your team wrote and
maintains, where you already own the blast radius. For that, declare the server outright:

```json
"mcp_trusted_servers": ["mcp__github__", "mcp__internal__"]
```

Every call to those servers is allowed: irreversible verbs, landing verbs, and mutating arguments
included. That is the point — a half-grant refuses exactly what you most likely built the server to
do, and an agent that stops to ask for the same `approve` on every pull request is not being made
safer, it is being made useless.

Whole servers only (`mcp__server__`), never a single tool; a malformed entry is refused at
config-read rather than silently dropped. `mcp_writes: "disabled"` still outranks it. Every call is
logged as `trusted_mcp_write`, and `status` reports the list **in capitals**, because a door this
wide that nobody can see is the failure this whole tool argues against.

What it cannot know: whether the server really is yours. Nothing checks authorship — only that
somebody with commit access to your config said so. If that config is shared, this is shared with it.

**The waiting probe**, if you fan work out to subagents: `watchdog.waiting_probe` is a command you
supply that answers *"is this run waiting on work it dispatched?"* Without it, a run that has
correctly handed out all its work looks identical to one that fell asleep, and eventually pages you.
It is config-only on purpose — a wait the agent could declare for itself would be an off switch for
the watchdog. The contract is three-valued:

| Exit | Meaning |
|---|---|
| `0` | Waiting. The watchdog stays quiet. |
| `1` | Not waiting — there is work here. The watchdog rings. |
| anything else, a timeout, or an unrunnable command | **Could not answer.** Still rings, *and* `status` reports the probe as FAILING. |

That third state exists because a probe that crashed and a probe reporting work produced identical
output, so a broken one stayed invisible for exactly as long as there was work to do. Write yours to
resolve its own dependencies explicitly: a hook's `PATH` is not your shell's, and a tool found by
bare name is the usual way one of these silently stops running.

The probe is told **which session it speaks for**, which matters as soon as one checkout holds more
than one session: `GAME_LOOP_SESSION`, `GAME_LOOP_SESSION_DIR`, and `GAME_LOOP_TRANSCRIPT`. Without
them a probe can only look across *every* session sharing the checkout, so it answers "waiting"
because somebody else's work is live — a **false waiting**, which is the one direction that fails
silent. An empty `GAME_LOOP_SESSION` means *unknown*, never a session named `""`; a probe that needs
scoping should exit 2 rather than guess.

**`GAME_LOOP_SESSION` is usually the one you want, and the transcript usually is not.** For the
common case — a parent waiting on subagents it dispatched — the parent's own transcript is quiet
*precisely while it waits*, so it is the state you are trying to recognise rather than a signal that
distinguishes anything. What is live during a fan-out is the subagents' artifacts, and those sit
under the **host's** per-session directory, not game_loop's. The session id is what lets a probe
resolve its own directory there instead of globbing across every session in the checkout.
`GAME_LOOP_TRANSCRIPT` is supplied for probes that genuinely want the parent's own activity.

### If a package manager installs game_loop for you

`status` tells you when a newer game_loop is on main. By default it suggests re-running the curl
installer — which is right for a curl install and **wrong for a vendored one**, where it would
replace a blessed, stamped release with whatever is on main at that instant and drop the
`CONFIDENCE` file. A correct alert with a destructive fix attached.

So a packager should drop `.game_loop/installed-by.json` beside the payload:

```json
{"name": "yourpkg", "upgrade": "yourpkg upgrade game_loop"}
```

The notice then names *that* command and stops offering the curl. The command is **printed, never
run** — a file game_loop executed would be a code-execution vector wearing a helpful face, so the
decision and the typing stay with the human. An unreadable or multi-line value falls back to the
ordinary notice rather than printing something nobody can trust.

### Site wiring: `config.local.json`

`.game_loop/config.local.json` is gitignored and layered on top of `config.json`, key by key. Put
anything machine- or checkout-specific there — a `waiting_probe` naming your tracker, a local write
root — so it never seeds into anyone else's install. `status` names how many keys are overridden and
which, because a config you cannot see is a divergence nobody can explain.

### Machine-wide trust: `~/.game_loop/config.json`

A third layer, read by every project's write guard and MCP guard (full install or `--central`) in
addition to its own `config.json` + `config.local.json` — for a grant you want to make *once*, for
this machine, rather than re-declare in every project. `templates/global-config.json` in this repo is
a starting point; nothing installs it automatically — copy what you need to `~/.game_loop/config.json`
by hand.

The trust-list keys (`read_roots`, `allow_write_roots`, `deploy_verbs`, `generated_globs`,
`mcp_read_only_tools`, `mcp_standing_writes`, `mcp_trusted_servers`) **union** across all three files
instead of replacing — a global grant can't be silently erased by a project's own (possibly absent)
same-key list, and a project's own grant survives a global file that never mentions it. Everything
else uses normal later-wins override, so a project can still narrow a global default (e.g. `mcp_writes`).

**One real limit, not silently swept under**: this only reaches projects that already run some form of
game_loop. A project with no `.game_loop/` at all never invokes any guard script in the first place, so
nothing in `~/.game_loop/config.json` reaches it — there is currently no global, no-install-required
layer.

### Prose that quotes code goes through a file

Every option taking free prose has a `--<name>-file PATH` twin, and an inline value over 400
characters is refused in favour of it — `checkpoint --notes-file`, `harden --general-file`, and so
on. A quoted shell argument is code to the shell first: backticks run, `$NAME` expands to nothing, a
lone quote truncates, and the tool receives the result with no way to know anything went missing.
The bound is measured against this repo's own logged prose, not chosen for roundness.

### Triggers: attaching your own actions to the loop

`.game_loop/triggers.json` (gitignored; see `templates/triggers.example.json`) lets a project hang
its own command on a moment in the loop. Nothing is attached by default.

| Event | Fires when | Typically used for |
|---|---|---|
| `harden` | a learning was just encoded into an artifact | share the transferable form with other agents |
| `stepback` | a retro just began | pull in what others learned; re-read the work queue |
| `confidence` | a commit was just marked | publish it wherever consumers take it |

A trigger gets a JSON payload on stdin and its stdout comes back to the agent. It **never blocks**
the verb, it is **never silent** — `status` lists each one with its last run, and a failing trigger
is shown as FAILING with its error rather than passing quietly.

Your project's own rules live in three files the installer seeds once and never overwrites:
`.game_loop/INVARIANTS.md` (your non-negotiables), `.game_loop/verify.yaml` (what a change owes), and
`.game_loop/config.json`.

## Flair 🎮 (fun, opt-out)

`game_loop` narrates the run like the game master of a dungeon crawl — your AI is the **Crawler**. When
a guard helps, it hands the agent a first-person line to repeat back, like *"🎮 GameLoop yanked me back
onto the path before the walls closed in. Back to it."* At milestones it hands out achievements and,
like any decent dungeon, runs a sponsor read:

```
🎮🏆 GameLoop has kept your Crawler alive and moving for 4h — not one game-over!
📺 This floor of the dungeon is sponsored by SupposedlySam. GameLoop encourages
   tribute → https://github.com/sponsors/SupposedlySam
```

Pure decoration, isolated in `.game_loop/bin/flair.py`, never touching gate logic, and completely
disabled by `flair.enabled: false` — set `support_name` / `support_url` to point the sponsor link
wherever you like.

## Migrating from an existing `.loop/`-style harness

1. `./install.sh /path/to/that/project` — adds `.game_loop/` and merges game_loop's hooks.
2. Move project-specific rules into `.game_loop/INVARIANTS.md`, `.game_loop/config.json`, and
   `.game_loop/verify.yaml`.
3. Delete the old `.loop/` directory **and its hook entries** from `.claude/settings.json`. The
   installer *adds* game_loop's hooks; it does not remove yours, so old ones would run alongside.
4. `./.game_loop/bin/game_loop status` to confirm.

## Installing / distribution

Distributed as this GitHub repo: clone it and run `install.sh` against your project, or use the
one-liner above. See [docs/distribution.md](docs/distribution.md) for the other channels under
consideration and the tradeoffs.

## Lineage & credit

game_loop is extracted from two harnesses that already ran unattended for real work — one where the
expensive gated action was a physical device flash (a human button-press), and one where it was a
real-money trade. Same `arm → gate → consume` primitive, same `VERIFIED / RULED-OUT / OPEN` ledger
vocabulary, two unrelated domains. game_loop is that pattern with the domain specifics removed, so
anyone can drop it into any project.

## License

MIT. See [LICENSE](LICENSE).
