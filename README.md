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
  are classified before they run, and anything unclassifiable is refused.
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
| `authorize --path <prefix> --reason ".."` | Your one-time, logged permission for a single write outside the repo. |

## Configure

Everything lives in `.game_loop/config.json` — read roots, extra write roots, deploy verbs to block,
watchdog timing, usage-limit thresholds. One worth knowing about if you fan work out to subagents:
`watchdog.waiting_probe` is a command you supply that answers *"is this run waiting on work it
dispatched?"* — exit 0 for yes. Without it, a run that has correctly handed out all its work looks
identical to one that fell asleep, and eventually pages you about it. It is config-only on purpose:
a wait the agent could declare for itself would be an off switch for the watchdog. It ships with sane defaults and comments; see
[docs/how-it-works.md](docs/how-it-works.md) for what each knob changes.

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
