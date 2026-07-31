# game_loop

**Guardrails that let a Claude Code session run unattended — safely.**

Think of it as a dungeon-crawl loop for your AI: `game_loop` doesn't play the game for the agent — it
keeps the run alive and stops it from wiping. It gives a Claude Code session two things it needs to
work without a human sitting over it:

- **an autonomy engine** that keeps the session moving instead of stopping the moment there's nobody to
  press "continue"; and
- **guardrails** that make running unattended *safe* rather than reckless.

All of it is enforced through Claude Code **hooks** — never through instructions to the model. The one
rule everything follows from:

> **Enforcement lives in tools and artifacts, never in instructions.** Test any guard by asking: *if
> the agent ignored every instruction, would this still hold?* If no, it isn't enforcement — it's a
> wish, and long sessions and context compaction break wishes.

That's why the keystone check is always the same shape: **name a real file that exists.** An LLM
defeats any check on the mere *presence* of a string by writing a plausible string — that's its native
skill. Pointing at a file on disk is the one check prose can't satisfy.

> 🤖 **If you're an AI agent**, read [`llms.txt`](llms.txt) — it's the operational brief: the exact
> commands to run and the gates you'll hit.

---

## Requirements

- **Python 3** (standard library only — no packages to install)
- **[Claude Code](https://claude.com/claude-code)** (the hooks are Claude Code hooks)
- macOS or Linux (`bash` + POSIX tools)

## Quickstart for humans

One-liner — no clone needed (the installer fetches the payload from GitHub itself):

```bash
curl -fsSL https://raw.githubusercontent.com/SupposedlySam/game_loop/main/install.sh | bash -s -- .
```

`bash -s -- .` installs into the current directory; pass any path in its place. Or, from a clone:

```bash
git clone https://github.com/SupposedlySam/game_loop.git
cd game_loop
./install.sh /path/to/your/project
```

Either way, `install.sh` copies `.game_loop/` into your project and merges the hooks into its
`.claude/settings.json` (it won't clobber existing settings or duplicate on re-run). Then:

> **Start a new Claude Code session before step 3.** Claude Code reads hook configuration when a
> session *starts*, and `install.sh` writes `.claude/settings.json` after yours already did — so in
> the install session every gate is registered on disk and silently never invoked. Nothing errors;
> the gate is just as quiet as a gate that's running and content. `game_loop status` prints a
> `HOOKS NOT LIVE` warning until the Stop gate has actually fired once.

```bash
cd /path/to/your/project
$EDITOR .game_loop/INVARIANTS.md      # 1. your project's north star (edit the template)
$EDITOR .game_loop/config.json        # 2. read roots, allowed write roots, deploy verbs, timing
./.game_loop/bin/game_loop status        # 3. sanity check — you should see the dashboard
```

That's it. The gates are inert until you bind a mandate, so day-to-day work is unchanged — the guards
only ever stop you from writing outside the repo or firing a configured deploy verb.

## Quickstart for agents (bots)

If you're a Claude Code agent working in a repo that already has `.game_loop/`, your whole operating
manual is [`llms.txt`](llms.txt). The short version:

```bash
./.game_loop/bin/game_loop status                                    # first thing, every session
./.game_loop/bin/game_loop claim --assert "X does Y" --read <path>   # before asserting external facts
```

If `.game_loop/` isn't there yet, install it from a clone of this repo: `./install.sh <this-project>`.

### Optional: let bare `game_loop` resolve without the path

The binary is intentionally **project-local** (`./.game_loop/bin/game_loop`) — there is no global
install, so an agent that assumes `game_loop` is on `PATH` (e.g. one arriving through a global slash
command that never loaded the project's `CLAUDE.md`) hits `command not found`. If you'd like bare
`game_loop …` to work from anywhere inside a game_loop repo, add this to your shell profile
(`~/.zshrc` / `~/.bashrc`):

```bash
game_loop() {
  if [ -x "./.game_loop/bin/game_loop" ]; then ./.game_loop/bin/game_loop "$@"
  else command game_loop "$@"; fi
}
```

It resolves to the **local** binary whenever you're in a game_loop repo, so it stays project-scoped —
no global binary that could point at the wrong project. This is opt-in on purpose: game_loop never
writes to your shell profile or puts anything on `PATH` for you.

## Run unattended

A human (or the agent, if the human said "work autonomously") binds a mandate:

```bash
./.game_loop/bin/game_loop mandate --set "Finish the timeline feature; pick the highest-value item and keep going."
```

While a mandate is bound:

- The **Stop gate** refuses turn-ends that ask a question, or that claim "continuing now" and then
  stop. The session either keeps working or explicitly `checkpoint`s / `arm`s / `clear`s.
- The **watchdog** notices when the session goes idle with work still outstanding and rings it back to
  work — so it resumes with no human present.

A mandate binds **the session that set it** — state lives per Claude Code session
(`.game_loop/sessions/<id>/state.json`), so two sessions sharing one checkout never see each other's
mandate, checkpoint, arm, or authorizations. Your other session opening the same repo will not be
conscripted into this one's work. Outside any session (your own terminal), state falls back to the
repo-global `.game_loop/state.json`.

When the work is genuinely done:

```bash
./.game_loop/bin/game_loop mandate --clear --notes "timeline shipped + verified"
```

With no mandate bound, every gate is inert — game_loop never sits between you and a normal conversation.

## Page your phone, not your terminal (Slack, optional)

An unattended run is autonomous enough that nobody should babysit it — so when it *does* need you,
the signal must reach where you actually are. Give game_loop a Slack channel and the tools (never the
model's memory) page it at exactly the moments that matter:

- **`arm`** — the run has a genuine T3 question, or needs your physical presence
- **watchdog stand-down** — the ring cap is exhausted; the run is stuck
- **`mandate --clear`** — the work is done
- **usage limits** — a window is nearly exhausted (handoff demanded), the run parks, the run resumes

Setup: create `.game_loop/notify.json` (gitignored — credentials never land in git):

```jsonc
{ "slack": { "bot_token": "xoxb-...", "channel": "C0123456789" } }   // send + read replies
// or send-only: { "slack": { "webhook_url": "https://hooks.slack.com/services/..." } }
```

then verify with `./.game_loop/bin/game_loop notify --test`. The bot-token form needs `chat:write` +
`channels:history` scopes and the bot invited to the channel; full schema and per-event tuning live in
the [`notify.py`](.game_loop/bin/notify.py) docstring.

**Replies flow back.** On the bot-token path, an `arm` page keeps its Slack thread: while the arm is
live, the watchdog polls that thread, and when you answer *from your phone*, it clears the arm and
rings your answer straight into the run. The desk is optional. (Webhooks are write-only, so there the
page is one-way.) A notification failure never takes down a gate — same contract as flair: Slack being
down means less decoration, never less enforcement.

## Survive Claude Code usage limits

Subscription usage is gated by rolling windows (a ~5-hour block and a 7-day block). Without help, a
run that hits one dies **mid-action** — everything it knew evaporates — and nothing restarts it when
the window resets. game_loop closes both holes, using the one place Claude Code actually exposes the
numbers: the statusline payload.

1. **The tap** — the installer wires `game_loop statusline` as your status line (only if you have
   none). Besides rendering a row (`🎮 model · ctx 45% · 5h 23% ↺14:32 · 7d 41% ↺Mon 09:00`), it
   snapshots `rate_limits` to `.game_loop/limits.json` on every refresh.
2. **The handoff gate** — a `PreToolUse` hook (`game_loop limitgate`) watches the snapshot. When a
   window crosses `limits.threshold_pct` (default 98%), ordinary tool calls are refused until the
   session writes its handoff — where it is, what's verified, what was planned next. The keystone is
   the usual one: a real file must exist. Handoffs are per session
   (`.game_loop/sessions/<id>/HANDOFF.md`; the gate's message names the exact path), so concurrent
   runs sharing a checkout never overwrite each other's, and one session's handoff never opens the
   gate for a sibling. Then work continues until the wall actually hits.
3. **The wake-up** — when the snapshot shows a window at `limits.exhausted_pct` (default 99%), the
   watchdog stops ringing (a ring is an API call into the very wall that killed the run), pages you
   that it parked, sleeps until `resets_at`, and then rings the session awake pointing at the handoff.
   The run continues on its own, minutes after the window resets.

Honest limits of the mechanism: the data exists only for Claude.ai subscribers (API-key auth exposes
nothing — every gate fails open), the per-model weekly limit is not in the payload, and the wake-up
revives a rate-limited *session*, not a quit app or a closed laptop. `game_loop status` shows the
current snapshot, and the handoff doubles as a human-readable "where did the run land" note — useful
even if you never automate the resume.

## The verbs

| Command | What it does |
|---|---|
| `game_loop status` | Rehydrate the loop after compaction. Run first, every session. |
| `game_loop mandate --set ".."` / `--clear` | Bind / release an autonomy mandate (arms the Stop gate + watchdog). |
| `game_loop mandate --park --reason ".."` / `--resume` | A HUMAN called a break: pause without closing. The mandate stays open work; one turn-end, consumed. |
| `game_loop checkpoint --notes ".."` | End a turn to *report* progress (no question). One turn-end, consumed. |
| `game_loop arm --question .. --read .. --predict ..` | Arm one interruption of the human, backed by a file you already read. |
| `game_loop claim --assert ".." --read <path>` | Assert something about external reality — refused unless you name a real file. |
| `game_loop claim --assert ".." --outcome refuted --evidence <path>` | Retract it: a first-class negative result. Must name the control that killed it; `status` keeps the RULED-OUT list. |
| `game_loop harden --learning .. --artifact <path> --mechanism .. --rung N` | Turn a learning into an enforced artifact. |
| `game_loop pin --fact .. --reason .. --path <path> [--expect ..]` | Carry a load-bearing environment fact (a pinned dep commit, a toolchain, an SDK path) in resume state, so a later tidy-up can't silently revert it. `--list` / `--release <id> --notes ".."`. |
| `game_loop authorize --path <prefix> --reason ".."` | One-time, logged permission for a single write outside the repo. |
| `game_loop trans --tier .. --milestone .. --doing ..` | Record a phase transition (drives the retro nudge). |
| `game_loop stepback --notes ".."` | Retro; re-injects your invariants. |
| `game_loop note --text ".."` | Append a note to the log. |
| `game_loop notify --text ".."` / `--test` | Page the configured Slack channel by hand / verify the channel works. |

## The guardrails

- **Claim gate** — can't assert about a dependency / harness / other repo without naming the real file
  you read.
- **Write guard** (`guard-writes.sh`, a `PreToolUse` hook) — an *allowlist*: writes are permitted only
  inside the repo, the OS temp dir, and configured roots. Everything else is read-only. Covers
  `Write`/`Edit` and Bash mutators, blocks configured deploy verbs, and states what it *doesn't* catch.
  Escape hatch is the human (`game_loop authorize`), single-use and logged — never an env var.
- **Blast-radius warning** (same guard, at `git commit`) — names the staged files this session never
  wrote, so a directory-wide formatter or `git add -A` can't quietly widen a commit past the work.
  A warning, never a block, and it says which edits it cannot see (Bash heredocs, `sed -i`, scripts).
- **verify** — optional map from "you changed X" to "these checks must pass"; refuses a `git commit`
  when the evidence is older than the change. Ships empty (a no-op) until you add rules.

See **[docs/how-it-works.md](docs/how-it-works.md)** for the full design, and **[`test/run.py`](test/run.py)**
for the guarantees as runnable checks (`python3 test/run.py`).

## Configure

`.game_loop/config.json`:

```jsonc
{
  "project_name": "my_project",
  "read_roots": [],          // extra bases for RELATIVE `claim --read` paths (deps, reference repos);
                             // absolute paths to real files already pass — the check is existence, not containment
  "allow_write_roots": [],   // extra dirs the write guard permits (beyond repo + OS temp)
  "deploy_verbs": [],        // extra irreversible verbs to block anywhere, e.g. "firebase deploy"
  "trans_nudge_every": 12,   // phase transitions between retro nudges
  "watchdog": { "idle_sec": 30, "settle_sec": 5, "ring_cap": 3 },
  "limits": {                // usage-limit survival (see "Survive Claude Code usage limits")
    "threshold_pct": 98,     // handoff gate closes here
    "exhausted_pct": 99,     // watchdog parks here and wakes the run at the window reset
    "handoff_file": "HANDOFF.md"   // resolved relative to .game_loop/
  },
  "update_check": true,      // status flags when the installed game_loop is behind main (re-install due)
  "update_repo": "SupposedlySam/game_loop",   // source repo the check compares against
  "flair": {                 // fun celebration lines (see below) — set enabled:false to silence
    "enabled": true,
    "support_name": "SupposedlySam",
    "support_url": "https://github.com/sponsors/SupposedlySam"
  }
}
```

## Flair 🎮 (fun, opt-out)

`game_loop` narrates the run like the game master of a dungeon crawl — your AI is the **Crawler**. When
a guard helps (the watchdog drags the Crawler back in, the Stop gate refuses a rage-quit, a claim gets
sourced) it hands the agent a first-person line to repeat back, like *"🎮 GameLoop yanked me back onto
the path before the walls closed in. Back to it."* At milestones it hands out achievements and, like
any decent dungeon, runs a sponsor read:

```
🎮🏆 GameLoop has kept your Crawler alive and moving for 4h — not one game-over!
📺 This floor of the dungeon is sponsored by SupposedlySam. GameLoop encourages
   tribute → https://github.com/sponsors/SupposedlySam
```

Milestones fire once each: uptime under a mandate (1h, 2h, 4h, 8h, …), total assists (5, 10, 25, 50,
…), claims sourced, and learnings hardened. The announcements and the sponsor reads are drawn from
rotating pools in a Dungeon-Crawler-Carl-style announcer register, so the ask never reads like the
same canned banner twice. It's pure decoration, isolated in `.game_loop/bin/flair.py`, never touches
the gate logic, and is completely disabled by `flair.enabled: false` — set `support_name` /
`support_url` to point the sponsor link wherever you like.

## Migrating from an existing `.loop/`-style harness

game_loop is the generalized descendant of hand-rolled loop harnesses. To switch one over:

1. `./install.sh /path/to/that/project` — adds `.game_loop/` and merges game_loop's hooks.
2. Move any project-specific rules into `.game_loop/INVARIANTS.md`, `.game_loop/config.json` (read/write
   roots), and `.game_loop/verify.yaml`.
3. Delete the old `.loop/` directory **and its hook entries** from `.claude/settings.json`. The
   installer *adds* game_loop's hooks; it does not remove yours, so old Stop/PreToolUse hooks must be
   pulled out by hand or they'll run alongside game_loop's.
4. `./.game_loop/bin/game_loop status` to confirm.

## Installing / distribution

Today game_loop is distributed as this **GitHub repo**: clone it and run `install.sh` against your
project. See [docs/distribution.md](docs/distribution.md) for the other channels under consideration
(a `curl | bash` one-liner, a Claude Code plugin, PyPI/`pipx`) and the tradeoffs.

## Lineage & credit

game_loop is extracted from two harnesses that already ran unattended for real work — one where the
expensive gated action was a physical device flash (a human button-press), and one where it was a
real-money trade. Same `arm → gate → consume` primitive, same `VERIFIED / RULED-OUT / OPEN` ledger
vocabulary, two unrelated domains. game_loop is that pattern with the domain specifics removed so
anyone can drop it into any project.

## License

MIT. See [LICENSE](LICENSE).
# game_loop
