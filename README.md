# bumper_bot

**Guardrails that let a Claude Code session run unattended — safely.**

Bumpers on a bowling lane don't roll the ball; they keep it out of the gutter. `bumper` does the same
for an autonomous agent. It gives a Claude Code session two things it needs to work without a human
sitting over it:

- **an autonomy engine** that keeps the session moving instead of stopping the moment there's nobody to
  press "go"; and
- **safety bumpers** that make running unattended *safe* rather than reckless.

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

```bash
git clone https://github.com/SupposedlySam/bumper_bot.git
cd bumper_bot
./install.sh /path/to/your/project
```

`install.sh` copies `.bumper/` into your project and merges the hooks into its
`.claude/settings.json` (it won't clobber existing settings or duplicate on re-run). Then:

```bash
cd /path/to/your/project
$EDITOR .bumper/INVARIANTS.md      # 1. your project's north star (edit the template)
$EDITOR .bumper/config.json        # 2. read roots, allowed write roots, deploy verbs, timing
./.bumper/bin/bumper status        # 3. sanity check — you should see the dashboard
```

That's it. The gates are inert until you bind a mandate, so day-to-day work is unchanged — the guards
only ever stop you from writing outside the repo or firing a configured deploy verb.

## Quickstart for agents (bots)

If you're a Claude Code agent working in a repo that already has `.bumper/`, your whole operating
manual is [`llms.txt`](llms.txt). The short version:

```bash
./.bumper/bin/bumper status                                    # first thing, every session
./.bumper/bin/bumper claim --assert "X does Y" --read <path>   # before asserting external facts
```

If `.bumper/` isn't there yet, install it from a clone of this repo: `./install.sh <this-project>`.

## Run unattended

A human (or the agent, if the human said "work autonomously") binds a mandate:

```bash
./.bumper/bin/bumper mandate --set "Finish the timeline feature; pick the highest-value item and keep going."
```

While a mandate is bound:

- The **Stop gate** refuses turn-ends that ask a question, or that claim "continuing now" and then
  stop. The session either keeps working or explicitly `checkpoint`s / `arm`s / `clear`s.
- The **watchdog** notices when the session goes idle with work still outstanding and rings it back to
  work — so it resumes with no human present.

When the work is genuinely done:

```bash
./.bumper/bin/bumper mandate --clear --notes "timeline shipped + verified"
```

With no mandate bound, every gate is inert — bumper never sits between you and a normal conversation.

## The verbs

| Command | What it does |
|---|---|
| `bumper status` | Rehydrate the loop after compaction. Run first, every session. |
| `bumper mandate --set ".."` / `--clear` | Bind / release an autonomy mandate (arms the Stop gate + watchdog). |
| `bumper checkpoint --notes ".."` | End a turn to *report* progress (no question). One turn-end, consumed. |
| `bumper arm --question .. --read .. --predict ..` | Arm one interruption of the human, backed by a file you already read. |
| `bumper claim --assert ".." --read <path>` | Assert something about external reality — refused unless you name a real file. |
| `bumper harden --learning .. --artifact <path> --mechanism .. --rung N` | Turn a learning into an enforced artifact. |
| `bumper authorize --path <prefix> --reason ".."` | One-time, logged permission for a single write outside the repo. |
| `bumper trans --tier .. --milestone .. --doing ..` | Record a phase transition (drives the retro nudge). |
| `bumper stepback --notes ".."` | Retro; re-injects your invariants. |
| `bumper note --text ".."` | Append a note to the log. |

## The bumpers

- **Claim gate** — can't assert about a dependency / harness / other repo without naming the real file
  you read.
- **Write guard** (`guard-writes.sh`, a `PreToolUse` hook) — an *allowlist*: writes are permitted only
  inside the repo, the OS temp dir, and configured roots. Everything else is read-only. Covers
  `Write`/`Edit` and Bash mutators, blocks configured deploy verbs, and states what it *doesn't* catch.
  Escape hatch is the human (`bumper authorize`), single-use and logged — never an env var.
- **verify** — optional map from "you changed X" to "these checks must pass"; refuses a `git commit`
  when the evidence is older than the change. Ships empty (a no-op) until you add rules.

See **[docs/how-it-works.md](docs/how-it-works.md)** for the full design, and **[`test/run.py`](test/run.py)**
for the guarantees as runnable checks (`python3 test/run.py`).

## Configure

`.bumper/config.json`:

```jsonc
{
  "project_name": "my_project",
  "read_roots": [],          // extra dirs where `claim --read` may resolve (deps, reference repos)
  "allow_write_roots": [],   // extra dirs the write guard permits (beyond repo + OS temp)
  "deploy_verbs": [],        // extra irreversible verbs to block anywhere, e.g. "firebase deploy"
  "trans_nudge_every": 12,   // phase transitions between retro nudges
  "watchdog": { "idle_sec": 30, "settle_sec": 5, "ring_cap": 3 },
  "flair": {                 // fun celebration lines (see below) — set enabled:false to silence
    "enabled": true,
    "support_name": "SupposedlySam",
    "support_url": "https://buymeacoffee.com/supposedlysam"
  }
}
```

## Flair 🎳 (fun, opt-out)

When a bumper actually helps — the watchdog rolls the agent back to work, the Stop gate keeps it on
track, a claim gets sourced — bumper hands the agent a fun first-person line to repeat back, like
*"🎳 Thanks for the nudge, BumperBot! Back to work."* At milestones it goes bigger:

```
🎳🏆 BumperBot has kept your AI rolling uninterrupted for 4h! If BumperBot is earning its
    keep, consider buying SupposedlySam a coffee ☕ → https://buymeacoffee.com/supposedlysam
```

Milestones fire once each: uptime under a mandate (1h, 2h, 4h, 8h, …), total assists (5, 10, 25, 50,
…), claims sourced, and learnings hardened. It's pure decoration, isolated in `.bumper/bin/flair.py`,
never touches the gate logic, and is completely disabled by `flair.enabled: false` — set
`support_name` / `support_url` to point the coffee link wherever you like.

## Migrating from an existing `.loop/`-style harness

bumper is the generalized descendant of hand-rolled loop harnesses. To switch one over:

1. `./install.sh /path/to/that/project` — adds `.bumper/` and merges bumper's hooks.
2. Move any project-specific rules into `.bumper/INVARIANTS.md`, `.bumper/config.json` (read/write
   roots), and `.bumper/verify.yaml`.
3. Delete the old `.loop/` directory **and its hook entries** from `.claude/settings.json`. The
   installer *adds* bumper's hooks; it does not remove yours, so old Stop/PreToolUse hooks must be
   pulled out by hand or they'll run alongside bumper's.
4. `./.bumper/bin/bumper status` to confirm.

## Installing / distribution

Today bumper is distributed as this **GitHub repo**: clone it and run `install.sh` against your
project. See [docs/distribution.md](docs/distribution.md) for the other channels under consideration
(a `curl | bash` one-liner, a Claude Code plugin, PyPI/`pipx`) and the tradeoffs.

## Lineage & credit

bumper is extracted from two harnesses that already ran unattended for real work — one where the
expensive gated action was a physical device flash (a human button-press), and one where it was a
real-money trade. Same `arm → gate → consume` primitive, same `VERIFIED / RULED-OUT / OPEN` ledger
vocabulary, two unrelated domains. bumper_bot is that pattern with the domain specifics removed so
anyone can drop it into any project.

## License

MIT. See [LICENSE](LICENSE).
