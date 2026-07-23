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

---

## Install

Requires Python 3 (stdlib only — no packages) and Claude Code.

```bash
git clone https://github.com/<you>/bumper_bot.git
cd bumper_bot
./install.sh /path/to/your/project
```

That copies `.bumper/` into your project and wires the hooks into its `.claude/settings.json`. Then,
in a Claude Code session in that project:

```bash
./.bumper/bin/bumper status          # rehydrate — run this first, every session
```

Re-running `install.sh` upgrades the scripts and re-merges the hooks without duplicating them, and
never clobbers your `config.json`, `INVARIANTS.md`, `verify.yaml` or notes.

## Run unattended

```bash
./.bumper/bin/bumper mandate --set "Finish the timeline feature; pick the highest-value item and keep going."
```

While a mandate is bound:

- The **Stop gate** refuses turn-ends that ask you a question, or that claim "continuing now" and then
  stop. The session either keeps working or explicitly `checkpoint`s / `arm`s / `clear`s.
- The **watchdog** notices when the session goes idle with work still outstanding and rings it back to
  work (via an `asyncRewake` Stop hook) — so it resumes with no human present.

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

See **[docs/how-it-works.md](docs/how-it-works.md)** for the full design.

## Configure

`.bumper/config.json`:

```jsonc
{
  "project_name": "my_project",
  "read_roots": [],          // extra dirs where `claim --read` may resolve (deps, reference repos)
  "allow_write_roots": [],   // extra dirs the write guard permits (beyond repo + OS temp)
  "deploy_verbs": [],        // extra irreversible verbs to block anywhere, e.g. "firebase deploy"
  "trans_nudge_every": 12,   // phase transitions between retro nudges
  "watchdog": { "idle_sec": 30, "settle_sec": 5, "ring_cap": 3 }
}
```

## Lineage & credit

bumper is extracted from two harnesses that already ran unattended for real work — one where the
expensive gated action was a physical device flash (a human button-press), and one where it was a
real-money trade. Same `arm → gate → consume` primitive, same `VERIFIED / RULED-OUT / OPEN` ledger
vocabulary, two unrelated domains. bumper_bot is that pattern with the domain specifics removed so
anyone can drop it into any project.

## License

MIT. See [LICENSE](LICENSE).
