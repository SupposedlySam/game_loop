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

The watchdog carries two more jobs, both riding the same asyncRewake wake mechanism:

- **Usage-limit park.** When `.game_loop/limits.json` (see the statusline tap below) shows a rate-limit
  window at `limits.exhausted_pct`, ringing is pointless — a wake-up is an API call into the very wall
  that killed the run — so it would only burn the ring cap against a dead wall. Instead the watchdog
  parks: it pages the human once, sleeps until the window's `resets_at` (re-checking the snapshot so an
  early roll-over or plan change ends the park as soon as the evidence does), then rings the session
  awake pointing at the handoff the limit gate demanded. What it misses, stated plainly: it revives a
  rate-limited *session*; if the human quit Claude Code there is no process left to wake.
- **Slack reply forwarding.** A T3 arm normally means "the human holds the ball — stay quiet". But when
  the arm was paged to Slack with a thread ts (bot-token setups), the ball can come back from a phone:
  the watchdog polls the thread while the arm is live, and on a human reply it clears the arm and rings
  the answer into the run. The trust scope is stated in `notify.py`: a reply is taken as the human's
  words — anyone in the channel can answer, so scope the channel accordingly.

### The statusline tap — `game_loop statusline`

Claude Code exposes subscription rate limits in exactly one place: the JSON it pipes to a configured
status line (`rate_limits.five_hour` / `.seven_day`, each `{used_percentage, resets_at}`) — no hook
event, headless flag, or state file carries them (sources in `LEDGER.md`). So the tap is a status
line command: on every refresh it snapshots those numbers to `.game_loop/limits.json` and renders a
one-row display. `install.sh` wires it only when no statusLine exists — a status line is the user's
front yard — and prints chaining instructions otherwise. Everything downstream (the limit gate, the
watchdog's park) reads the snapshot; a session that never rendered a status line has no snapshot, and
every consumer treats that as *absence of signal, not evidence of headroom*, and fails open.

---

## The guardrails

### The claim gate — `game_loop claim`

Before asserting anything about external reality (a dependency's behavior, a harness detail, another
repo), you must name the real file you read: `game_loop claim --assert "X does Y" --read path/to/file`.
It refuses unless the path names a real, non-empty file — an absolute path to any real file, or a path
relative to the repo or a configured `read_root`. The check is *existence*, not containment: citing a
sibling repo you actually read is exactly the point, so absolute paths outside the repo pass by design
(`read_roots` only add extra bases for resolving *relative* paths). This is the epistemic guardrail: it
stops the confident-but-unsourced assertion, which is
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

### The limit gate — `game_loop limitgate`

A second `PreToolUse` hook, watching `.game_loop/limits.json`. When a rate-limit window crosses
`limits.threshold_pct` (default 98%), the session is minutes from dying mid-action with everything it
knows still in its head — so the gate refuses ordinary tool calls until a handoff file exists
(per session: `.game_loop/sessions/<id>/HANDOFF.md`, so concurrent runs never overwrite each other's
and one session's handoff never opens a sibling's gate): where the run is, what is verified, what was
planned next. The keystone is
the usual shape — a real, non-empty file, written after the crossing. While closed, exactly the
handoff work stays allowed: `Write`/`Edit` to the handoff path and `game_loop` verbs. It fails OPEN on
any missing signal (no snapshot, a window that already reset) because a gate that blocks on absent
evidence blocks its own fix — and it says what it is not: a nudge that the handoff exist, not a
security boundary (the write guard still owns what may be mutated).

### The unpushed check — `checkpoint` / `mandate --clear`

Agents commit constantly and push rarely, and committed-but-unpushed work is invisible to everyone
except the agent that wrote it — which reports it as done, because locally it *is* done. So at the two
handbacks (`checkpoint`, `mandate --clear`) — the moments the human forms their picture of what
exists — game_loop says how far ahead of its upstream `HEAD` is, and by how many commits. It escalates
the wording when the notes describe finished work, a deploy, another person, or a handoff: those are
the cases where unpushed silently becomes someone else's confusing afternoon. It is a **warning, never
a block** (holding commits back is sometimes right), and a branch with **no upstream stays quiet** —
nobody was ever promised that branch. What it misses is stated in the code: uncommitted work, stashes,
other local branches, and the fact that "pushed" is not "merged".

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

### pins — `game_loop pin`

Environment state the build depends on is invisible to the harness, so a run cannot tell a stale
leftover from a load-bearing one. A dependency checkout gets moved to a non-default commit because
the work needs an API only present there; a later tidy-up restores it to its default branch and the
build dies on a symbol that "does not exist". Reverting unexplained local state is *good hygiene* —
that is exactly what makes the trap work, and nothing warns the tidying instinct off.

A pin puts the fact in resume state **with the reason it is load-bearing**, so `status` re-prints it
after every compaction and releasing it becomes a stated decision:

```
game_loop pin --fact "vendor/dep is at abc123 (not main)" \
              --reason "the merge API only exists on that commit" \
              --path vendor/dep/.git/HEAD --expect abc123 \
              --restore "git -C vendor/dep checkout abc123"
game_loop pin --list
game_loop pin --release p1 --notes "the API landed on main"
```

`--path` is the usual keystone and is mandatory — a file *or* a directory, since a pin's subject is
routinely a checkout or an SDK root. `--expect` is what raises a pin from *displayed* to *checked*:
status re-reads the anchor and prints `DRIFTED` when the text is gone. It must already hold at
registration, because a check born red is a check nobody believes later. What a pin **misses**,
stated in the code: with no `--expect` it only proves the anchor still exists, and the failure above
leaves a perfectly real directory behind — so an unchecked pin prints `UNCHECKED`, never `✓`.

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
| `bin/game_loop` | the CLI (all the verbs, plus the stopgate/limitgate/statusline hook entrypoints) |
| `bin/watchdog` | the autonomy engine (Stop hook): idle rings, limit park, Slack reply forwarding |
| `bin/guard-writes.sh` | the write guard (PreToolUse hook) |
| `bin/verify` | the changed-file → owed-checks gate |
| `bin/notify.py` | optional Slack paging (never enforcement; a Slack outage never breaks a gate) |
| `config.json` | read roots, allow-write roots, deploy verbs, watchdog + limits knobs |
| `notify.json` | Slack credentials + per-event paging config (git-ignored) |
| `limits.json` | the statusline tap's rate-limit snapshot (git-ignored; deliberately account-scoped — sessions share the subscription windows — with cross-session flock + monotonic merge on update) |
| `sessions/<id>/HANDOFF.md` | the limit gate's demanded handoff, PER SESSION (git-ignored; delete after re-absorbing it) |
| `sessions/<id>/state.json` | counters, phase, mandate, arms — PER Claude Code session (atomic writes; git-ignored) |
| `state.json` | the no-session fallback state (a human terminal, an older harness) |
| `log.jsonl` | append-only event log, shared across sessions; each line carries the writing session's `sid` (git-ignored) |
| `INVARIANTS.md` | your north star; re-injected by `game_loop stepback` |
| `verify.yaml` | the change → checks map |
| `LEDGER.md` | VERIFIED / RULED-OUT / OPEN reference (not a gate) |

Run `game_loop status` first thing every session — it rehydrates the cost ladder, invariants, counters,
and current phase from disk, which is how the loop survives context compaction.

### Why state is per-session

Two Claude Code sessions routinely share one checkout (a main session plus a side quest, a human plus
an unattended run). With one shared state file, a mandate bound by session A closes session B's Stop
gate and rings B's watchdog — and B, told "you are under a mandate with work outstanding", will go off
and *do A's work*. That happened. So every gate resolves the session first — hooks from the
`session_id` on their stdin payload, CLI verbs from `CLAUDE_CODE_SESSION_ID` (exported to the agent's
shell) — and reads only that session's `sessions/<id>/state.json`. No id at all (your own terminal, an
older harness) falls back to the repo-global `state.json`, which behaves exactly as before.
`GAME_LOOP_SESSION` overrides the id; set-but-empty (`GAME_LOOP_SESSION=`) deliberately targets the
repo-global file — that is how a leftover pre-per-session mandate gets cleared. Authorizations
(`game_loop authorize`) are session-scoped too: granted in a session, spendable only there.
