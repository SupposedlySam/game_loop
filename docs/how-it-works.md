# How game_loop works

game_loop is a dungeon-crawl loop for a Claude Code session: it doesn't play the game for the agent,
it keeps the run alive and stops it from wiping. Two forces work together.

- **The autonomy engine** keeps the session moving when there is no human to press "continue".
- **The guardrails** make running unattended *safe* rather than reckless.

Everything is enforced through Claude Code **hooks** — never through instructions to the model. The
one design rule, from which all of it follows:

> Enforcement lives in tools and artifacts, never in instructions. Test any guard by asking: *if the
> agent ignored every instruction, would this still hold?* If no, it isn't enforcement.

That is why the keystone check is always the same shape — **name a real file that exists.** An LLM
defeats any check on the mere presence of a string by writing a plausible string; that is its native
skill. Pointing at a file on disk is the one check prose cannot satisfy.

---

## The autonomy engine

Two hooks on the `Stop` event, which fires when the session is about to end its turn.

### 1. The Stop gate — `game_loop stopgate`

When a **mandate** is bound (`game_loop mandate --set "..."`), the gate inspects the agent's closing
message and blocks turn-ends that shouldn't happen yet:

- **Asking the human a question** while unarmed → blocked. Answer it yourself, or `game_loop arm` a real
  question backed by a file you already read that didn't answer it.
- **Announcing "continuing now" and then stopping** → blocked. That is a false statement about the
  agent's own state; either actually continue, or report honestly in past/future tense.
- **Any bare turn-end** under a mandate → blocked unless you `game_loop checkpoint --notes "..."`
  (report and hand back, no question) or `game_loop mandate --clear` (the work is done).

Exit 2 from a Stop hook feeds stderr back to the model as feedback, so a blocked turn-end lands as an
instruction to keep working.

With **no mandate bound the gate is inert** — it never sits between a human and a normal conversation.

### 2. The watchdog — `bin/watchdog`

The gate stops the session *saying* the wrong thing. It cannot make it *do* the next thing — a blocked
turn-end still needs someone to press go. The watchdog is that someone.

It arms as a **backgrounded** Stop hook (`asyncRewake: true`, long timeout). When game_loop state says
*(mandate bound, work outstanding, nobody waiting on the human)* but the harness says *(the transcript
hasn't grown in `idle_sec` seconds — the session is parked)*, that is a contradiction. It rings
(exit 2), and asyncRewake turns the ring into a model wake-up. The session picks the work back up.

Guardrails on the watchdog itself:

- **Newest wins** — a pidfile ensures only one watchdog is armed; a superseded one exits quietly.
- **Settle before measuring** — it waits `settle_sec` for the harness to finish flushing the turn
  before taking a transcript baseline, so it doesn't mistake a just-ended turn for activity.
- **Ring cap** — after `ring_cap` *consecutive unproductive* rings it stands down (a nag that never
  quits gets ignored). The moment the transcript grows after a ring, the budget resets: the ring
  worked.
- **Fails visibly** — every quiet exit logs *why*, so a broken watchdog is distinguishable from a
  correctly-silent one. If the harness ever stops honouring asyncRewake, the last-ring record in
  `game_loop status` is where the silence shows.

Tune all three knobs in `.game_loop/config.json → watchdog`.

---

## The guardrails

### The claim gate — `game_loop claim`

Before asserting anything about external reality (a dependency's behavior, a harness detail, another
repo), you must name the real file you read: `game_loop claim --assert "X does Y" --read path/to/file`.
It refuses unless the path resolves to a real, non-empty file — under the repo or a configured
`read_root`. This is the epistemic guardrail: it stops the confident-but-unsourced assertion, which is
the most expensive mistake an unattended agent makes because nobody is watching to catch it.

### The write guard — `bin/guard-writes.sh`

A `PreToolUse` hook enforcing an **allowlist**: writes are permitted only under the repo, the OS temp
dir, this project's agent-memory dir, and anything in `config.json → allow_write_roots`. Everything
else — other projects, your home directory, system files — is read-only by default. It covers
`Write`/`Edit`/`NotebookEdit` and Bash mutators (`rm`, `mv`, redirects, `git` writes, `sed -i`, …),
resolving paths with realpath and tracking `cd` across a command. It also blocks configured
deploy/publish verbs anywhere. It states what it does *not* catch (MCP tools, interpreter one-liners,
paths built from shell variables) right in the file — a guard that overstates its reach is worse than
one that states its limits.

The only way past it is the human, single-use and logged: `game_loop authorize --path <prefix> --reason
"<their words>"`.

### The arm → gate → consume primitive

The shape shared by every expensive action. You **arm** one spend, a hook **gates** on it, and using
it **consumes** the arm — so one authorization buys exactly one action, always logged. `game_loop arm`
(one interruption of the human) and `game_loop authorize` (one out-of-repo write) are both this.

### verify — `bin/verify`

Optional. A map (`.game_loop/verify.yaml`) from "you changed files matching `<glob>`" to "these commands
must pass", plus a record of when each last passed. `verify --check` refuses if a changed file is
newer than the last successful run of its checks — the gate is "is the evidence newer than the
change", not "did you remember". The write guard calls it before `git commit`. Ships empty (a no-op)
until you add rules.

### harden — `game_loop harden`

The meta-guard. When you learn something, you don't write it down — you `harden` it into an artifact
the system enforces, and the command refuses unless you name the real file that now enforces it. Docs
are the index; the artifact is the enforcement. Take the highest rung that applies (IMPOSSIBLE > LOUD
> CHECKED > AUTOMATED > VISIBLE > doc-of-last-resort).

---

## The files

Everything lives in `.game_loop/`:

| File | What it is |
|---|---|
| `bin/game_loop` | the CLI (all the verbs) |
| `bin/watchdog` | the autonomy engine (Stop hook) |
| `bin/guard-writes.sh` | the write guard (PreToolUse hook) |
| `bin/verify` | the changed-file → owed-checks gate |
| `config.json` | read roots, allow-write roots, deploy verbs, watchdog knobs |
| `state.json` | counters, phase, mandate, arms (atomic writes; git-ignored) |
| `log.jsonl` | append-only event log (git-ignored) |
| `INVARIANTS.md` | your north star; re-injected by `game_loop stepback` |
| `verify.yaml` | the change → checks map |
| `LEDGER.md` | VERIFIED / RULED-OUT / OPEN reference (not a gate) |

Run `game_loop status` first thing every session — it rehydrates the cost ladder, invariants, counters,
and current phase from disk, which is how the loop survives context compaction.
