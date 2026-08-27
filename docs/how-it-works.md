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

#### The one non-closure exit — `game_loop mandate --park`

A mandate has exactly two ends. `--clear` is **closure**: the work is done. `--park --reason "<their
words>"` is a **human-called break**: not done, not given up on, interrupted by the one authority the
gate exists to defer to. Before it existed, an interrupted run's only options were to violate the gate
or to fabricate a closure that reads forever after as though the work had been finished — so every
session improvised, and the log could not tell an externally-interrupted run from a self-terminated
one. Now `mandate_park` says so, in the human's own words, attributed to them.

A park is **not a way out of the gate**, structurally: it buys exactly one turn-end (consumed like a
checkpoint or an arm), the ask/announce checks still run first so it cannot launder a question, and it
clears nothing — `game_loop status` keeps showing the mandate as OPEN with the recorded next step
intact, and the next turn-end meets a live gate again. The watchdog stands down while parked, so the
break actually happens; only `mandate --resume` ends it. What it cannot enforce, plainly: nothing on
this side of the keyboard can verify the human really called the break — the agent is the one typing.
It makes the break loud, narrow, and permanently attributable, which is strictly more than the
`--clear` an interrupted run was already reaching for. (`park` is also the watchdog's word for waiting
out a usage limit — same meaning, different waker: the clock ends that one, the human ends this one.)

### 2. The watchdog — `bin/watchdog`

The gate stops the session *saying* the wrong thing. It cannot make it *do* the next thing — a blocked
turn-end still needs someone to press go. The watchdog is that someone.

It arms as a **backgrounded** Stop hook (`asyncRewake: true`, long timeout). When game_loop state says
*(mandate bound, work outstanding, nobody waiting on the human)* but the harness says *(the transcript
hasn't grown in `idle_sec` seconds — the session is parked)*, that is a contradiction. It rings
(exit 2), and asyncRewake turns the ring into a model wake-up. The session picks the work back up.

Guardrails on the watchdog itself:

- **Newest wins** — a pidfile ensures only one watchdog is armed; a superseded one exits quietly.
  The file records the process's *start time* beside its pid, and nothing signals a pid whose
  identity does not match. Nothing deletes that file when a watchdog exits, so a stale pid is
  the normal case rather than the rare one; if the OS has recycled it, the number alone cannot
  tell this session's watchdog from a stranger, and what follows the read is a SIGTERM. A pid
  that cannot be *shown* to be the watchdog is left alone and said so — "could not check" and
  "checked, it is ours" must not share a consequence when the consequence is killing something.
- **Settle before measuring** — it waits `settle_sec` for the harness to finish flushing the turn
  before taking a transcript baseline, so it doesn't mistake a just-ended turn for activity.
- **Ring cap** — after `ring_cap` *consecutive unproductive* rings it stands down (a nag that never
  quits gets ignored). The moment the transcript grows after a ring, the budget resets: the ring
  worked.
- **Fails visibly** — every quiet exit logs *why*, so a broken watchdog is distinguishable from a
  correctly-silent one. If the harness ever stops honouring asyncRewake, the last-ring record in
  `game_loop status` is where the silence shows.
- **Stands down after a handover** — once `successor` has started the next session, this one has given
  its mandate away and a ring would drag it back into work somebody else is doing. See the successor
  section; the flag is what stands the engine down, because a Stop hook arms a fresh watchdog at every
  turn-end.

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

### The wake path — `mandate --wake-path`, `note --woke`, `doorbell`

Every gate above fires from **inside** the session, which is the thing that stops working when a run
goes quiet. A consumer's run sat inert for six hours with the Stop gate, the watchdog and the limit
gate all reporting healthy, while their chat transport's own doctor said no wake had landed in that
whole time. Nothing here knew, and nothing here could.

`mandate --wake-path "<how a signal reaches this session>"` records that something outside can reach
the run, and `status` warns while nothing is recorded. It is a **declaration, never a probe**, and
says so where it is printed: this cannot see a host's cron and does not pretend to.

The other half is observable, and only once something arrives. `note --woke` records an arrival;
`status` then reports when the last wake **landed**, how long ago, and how many. Nothing else can
record it, so `doorbell` asks the woken run to do it first — an unrecorded arrival reads exactly
like a dead wake path.

**What it still cannot see, stated in the report itself:** a wake that was *requested and never
delivered* leaves nothing here, because the run that would have recorded it is the run that did not
happen. That is the six-hour hole, and only a watcher outside the session can close it.

`doorbell` prints the wake-up prompt for the run — what done means, where to resume, and the
recovery paths recorded with `note --recovery`. It is generated rather than a file you fill in,
because those paths are per-run: the known flake and its remedy, which credential actually
authenticates, the known-good retry. With none recorded it says outright that the prompt mostly buys
re-orientation, rather than printing a confident one that carries nothing. A generic "check your
background tasks" ping is nearly worthless — the agent wakes, spends real tokens re-deriving where
it was, and re-runs finished steps.

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

### Prose in, unmangled — `--<option>-file`

Every option that takes free prose has a `--<name>-file PATH` sibling, and an inline value over 400
characters is **refused** in favour of it.

This is not a style rule. A quoted shell argument is *code to the shell* before it is text to the
tool: backticks run as command substitution, `$NAME` expands to nothing, a lone quote truncates. The
shell substitutes and hands over the result — so by the time the tool sees the value, the evidence is
gone. A sentence with three words eaten out of it is just a shorter sentence, and no inspection can
tell it from one somebody wrote that way. It lands in a permanent record silently.

The bound is measured, not chosen: across this repo's own logged prose, 400 sits above the 95th
percentile of every field that is naturally a one-liner, and bites only the long-form ones — which
are exactly the ones that carry code, newlines and quoting. The two values corrupted here were both
over 700 characters.

What it does **not** fix: a short value with a backtick in it is still mangled and still accepted —
this bounds the exposure, it does not remove the class. And it reaches this tool only; `git commit -F`
and `gh --body-file` are the same answer for the other CLIs in the loop.

### The claim gate — `game_loop claim`

Before asserting anything about external reality (a dependency's behavior, a harness detail, another
repo), you must name the real file you read: `game_loop claim --assert "X does Y" --read path/to/file`.
It refuses unless the path names a real, non-empty file — an absolute path to any real file, or a path
relative to the repo or a configured `read_root`. The check is *existence*, not containment: citing a
sibling repo you actually read is exactly the point, so absolute paths outside the repo pass by design
(`read_roots` only add extra bases for resolving *relative* paths). This is the epistemic guardrail: it
stops the confident-but-unsourced assertion, which is
the most expensive mistake an unattended agent makes because nobody is watching to catch it.

A claim also records **how it landed** — `--outcome resolved` (the default) / `refuted` /
`inconclusive` — because being wrong is the result most worth keeping: the reason a dead path is not
re-walked in a later session is that the log says *this looked right, here is the control that killed
it*. A refutation must name that control (`--evidence <path>`), the same keystone turned on being
wrong, and it costs exactly one real path like any other claim — retracting already *looked* like
failure while costing what progress costs, and that asymmetry is what taught the quiet move-on.
`game_loop status` reads the standing **RULED OUT** list straight back out of the shared log, so a
resumed session inherits the negative results instead of rediscovering them. The list lives in the log
rather than in state on purpose: a negative result is knowledge about the *checkout*, and the run that
must not re-walk the dead path is usually a later session holding none of this one's state.

A claim about a **category** costs more than a claim about an instance. "Only X", "X is restricted",
"X does not support Y" are claims about a *set*, and a set is exactly what one observation cannot
establish — the observation is usually right and the scope is invented. (One table's DELETE returned
500; the run reported that table as restricted, the human relied on it, and an archive-instead-of-
delete scheme was built. Hours later every DELETE on every table returned 500: the path was down
everywhere, and one request against any other table would have shown it.) So
`claim --scope "<the category>" --probe <a> --probe <b>` demands two probes on **different** members
— a repeat of the first proves nothing — and records both, which is one more request in exchange for
a boundary instead of a guess. The flag is the enforcement. A wording check on the assertion is only
a **nudge**: when something set-shaped is filed as an instance, the claim is still admitted and the
second probe is offered loudly, because enforcement that depends on reading English is not
enforcement and a false positive must never block a legitimate claim. What the gate cannot check, in
its own output: that either probe was really run, or that the second member sits on the far side of
the category. It holds you to two members, not to the right two. The moment to reach for it is the
one the incident names — **you start building a workaround**, which is always downstream of a scope
claim and the last point at which the claim is cheap to check.

### The write guard — `bin/guard-writes.sh`

A `PreToolUse` hook enforcing an **allowlist**: writes are permitted only under the repo, the OS temp
dir, this project's agent-memory dir, and anything in `config.json → allow_write_roots`. Everything
else — other projects, your home directory, the OS's own files — is read-only by default. It covers
`Write`/`Edit`/`NotebookEdit` **by tool name**, every redirect form in the shell grammar that creates
or truncates a file, a **named** set of write-capable verbs (`rm`, `mv`, `cp`, `tee`, `dd`, … plus
`curl -o`, `wget -O`, `tar -C`, `unzip -d`, `patch -o`, `install`, `rsync`, `split`, `sed -i`,
`perl -i`) and `git` writes — resolving paths with realpath and tracking `cd` across a command. **The
authoritative list is the SCOPE block at the top of `guard-writes-impl.sh`**, which names every verb
rather than eliding them; this paragraph is a summary and the file is the contract.

That distinction was not free. Until 2026-08-26 both this paragraph and the file wrote the list as
`rm/mv/cp/…`, and the ellipsis is what a reader takes for "and the other obvious ones": five redirect
forms (`>|`, `>!`, `>>!`, `>&`, `>>&`) and nine verbs including `curl -o` and `tar -C` wrote outside
the repo unchecked, for weeks, while this page said the guard states its limits. It also blocks configured
deploy/publish verbs anywhere. It states what it does *not* catch (interpreter one-liners, paths built
from shell variables) right in the file — a guard that overstates its reach is worse than one that
states its limits.

The only way past it is the human, single-use and logged: `game_loop authorize --path <prefix> --reason
"<their words>"`.

**It leaves a mark, so that "allowed" and "never ran" stop being the same observation** (#41). A deny
is loud, but an allow is *silence* — and silence is exactly what a guard that checks nothing emits.
Replacing `guard-writes-impl.sh` with a script that parses and exits 0 (present, wired, live,
checking nothing — so the fail-open notice never fires either) left sixteen "allows…" assertions in
the suite green. A refusal cannot be produced by absence, so every *block* assertion validates
itself; it is specifically the permissive half that needs a second bit. So the guard advances a
counter at `sessions/<sid>/write-guard-probe` on **every** invocation, **before its first early
return** — the cheapest allows return soonest, and a mark written after them would leave exactly the
unproven cases unproven while looking like the pattern had been applied. Tests then require the mark
to have *advanced* as well as the tool to have been allowed. It costs one small read and one small
write per tool call, all bash builtins, and every step is silenced: a probe that cannot be written
costs the mark, never the guarding (INV5). It proves the script ran and got that far — not that any
particular check downstream was correct (INV6).

The generalisation is the reusable part, for any suite with a guard in it:

| the guard, when it permits | what a permissive test must assert |
|---|---|
| **speaks** (gives a reason) | the reason, not the verdict |
| **is silent** | that a mark it carries **advanced** |

Both are one requirement: a permissive assertion must observe evidence of *work*, because the verdict
alone is also what absence produces.

### The blast-radius warning — the same guard, at `git commit`

The commit gate below asks whether a change was *verified*. It never asked whether it was
*intended*, and one command widens a commit far past the work: a formatter aimed at a whole
directory reformatted a dozen files nobody had opened, `git add -A` swept them in, and the commit
message described something else. A commit's blast radius and the session's actual work are
different sets, and only the session knows the second one.

So the write guard records every path it allows through `Write`/`Edit`/`NotebookEdit` — one
repo-relative line per file in `sessions/<id>/edited.txt`, beside that session's state — and at
`git commit` compares it against the staged set, naming the excess and counting it. Generated and
vendored paths are exempt (lockfiles, `vendor/`, `node_modules/`, `*.g.dart`, … ; extend with
`config.json → generated_globs`), as is game_loop's own runtime state. It is a **warning, never a
block** — sweeping edits are sometimes exactly the intent — delivered as context on the tool call,
which is why the commit still proceeds untouched.

What it misses, stated in the guard itself: it only knows edits it saw as `Write`/`Edit`, so a file
written through Bash (a heredoc, `sed -i`, a script), by a sibling session, or before this session
started is not in the set and gets named as excess. With no recorded edits it says nothing, and it
reads the index — `git commit -a`, an explicit pathspec and `--no-verify` all pass unexamined.
Silence from it is not evidence that a commit is tight.

#### Provenance — `game_loop attribute --merge <ref>`

That "by a sibling session" case is not a rare edge; it is an orchestrator's *normal shape*. When the
session that writes the code and the session that lands it are different, `git merge` brings in files
this session never touched, and every one reads as excess. Observed live across ~14 integration
commits: the warning fired on 8, naming 2–10 legitimate files each time. A merge-**only** session is
already silent (no recorded edits ⇒ no accusation); the broken case is the **mixed** one — a few
edits of its own plus merges. And a warning that is wrong every time is one people learn to scroll
past, at which point it stops working for the case it was built for.

The session scoping is not the bug — an authorization is granted to a *session*, and must be
spendable across every tree it works in (INV5). The bug was that the check had no way to be told a
commit's **provenance**. So it can be, once, out loud:

```
game_loop attribute --merge <ref> [--merge <ref> ...] --reason "<why this commit carries them>"
```

The declaration names **refs, never filenames**, and game_loop recomputes the file set itself:
`git diff --name-only $(git merge-base HEAD <ref>)..<ref>`. That is this project's keystone — *cite
the file you read* — applied to attribution. A JSON array of filenames is exactly the plausible
string a model produces for free and nothing can check; a ref is real, resolvable, and **the
recomputation is the check**. A ref that does not resolve is refused, the same way `claim --read`
refuses a path that is not there. Then the check partitions staged files **three** ways: this
session's own edits, what a named ref carries, and what is in **neither** — and that third set
becomes the entire output. It is consumed by the next commit the check examines and written to
`log.jsonl` with its refs and reason, exactly like `authorize`.

This is **stricter, not quieter**. Today a file *nobody* wrote is one line among ten legitimate ones
in a warning everyone skips; after, it is the only line. What it still cannot check (INV6): that a
declaration was *honest* about intent. A real ref chosen to blanket a file resolves fine. It is
narrowed to one commit and permanently attributable instead — the most a guard on this side of the
keyboard can do.

Deliberately **not** solved with `config.json → generated_globs`, which was the shortcut sitting
right there: that list is keyed to *paths* rather than provenance, so it would suppress genuine
findings on those paths forever, it grows monotonically as more of a repo gets orchestrated, and it
lies — merged files are not generated.
### The MCP guard — `bin/guard-mcp.sh`

The write guard reads **Bash**. But a session with MCP servers connected can take an irreversible
action with no shell command at all — a `DELETE FROM …` through a database server, a send or a delete
through a mail/chat server, a force-operation through a git-host server. None of those arrive as Bash,
so a guard that only reads Bash never sees them; MCP tools are a first-class effector, on par with the
shell. That gap was *written down* in the write guard's scope notes for a long time, which is exactly
the failure this project exists to prevent: **a gap stated in prose is a gap that gets walked
through**, because the note doesn't stop anything (INV1).

So a second `PreToolUse` hook, matched on `mcp__.*`, classifies the call **before** it runs. Nothing
tells a client which of a server's tools mutate, so it matches the two things that *are* observable —
the tool **name** and the **argument shape**: read-only verbs (`get`, `list`, `query`, …) pass;
mutating or irreversible verbs (`delete`, `send`, `push`, `merge`, `deploy`, `force`, …) are refused,
and so is any call whose arguments carry a mutating SQL statement, a destructive flag (`--force`, a
truthy `force:`), or a mutating request method. **The argument always wins**, so a read-named `query`
tool carrying `DELETE FROM` is still blocked. Same escape hatch as the shell mutators, spelled with
the tool name: `game_loop authorize --path mcp__<server>__<tool> --reason "<their words>"`.

Anything it cannot classify is **refused** — it fails **closed**, the opposite default from the write
guard, and the reason for the difference is scope. The write guard is matched on
`Write|Edit|NotebookEdit|Bash`, so a broken write guard blocks the very edit that would repair it and
the session can only be rescued from outside (INV5); it must fail open. This one is matched on
`mcp__.*` only. A refusal here never blocks its own fix, and what is on the other side of an
unclassifiable MCP call is a delete, a send, or a publish. Where failing closed is free, a guard that
guesses "probably fine" is not a guard. A project can teach it the ambiguous tools of a server it
trusts (`config.json → mcp_read_only_tools`) — a list that can only resolve ambiguity, never silence a
mutating verb or a mutating argument.

Ask-every-time is the wrong shape when a workflow's **work product** lands through an MCP write — a
finished review that cannot be posted buys a retry, not a safety decision. So a project may state a
standing policy, the MCP analogue of `allow_write_roots`: `config.json → mcp_standing_writes`, in
either of two grains and nothing between them.

```json
"mcp_standing_writes": ["mcp__github__", "mcp__other__createThing"]
```

An **exact** `mcp__server__tool` names one tool. A **whole-server** `mcp__server__` prefix trusts a
server — the right unit when the server is the project's own first-party code, because enumerating
its tools goes stale every time it grows one, and it goes stale toward a *dead-ended agent*. A prefix
is safe here for a reason specific to this guard rather than a promise: every floor above runs on the
**live call**, from the tool being invoked, and returns before the standing policy is consulted. So a
prefix widens *which servers* are trusted; it cannot widen *what* may be done through them. An
argument-level finding still refuses, an irreversible verb still refuses, `mcp_writes: "disabled"`
still makes the whole list inert, and every consumption is logged — with which grain allowed it, so
an audit can review the rule rather than just observe that something permitted the call.

One tier a prefix does **not** inherit: `merge`, `publish`, `deploy`, `release`, `push`. Those still
need the tool named exactly. Under an enumeration, granting one was the deliberate act of typing it
out; under a prefix it would be inherited from which list a verb happens to sit in — and that is the
difference between an agent posting its review unattended and an agent landing code unattended.

What it still cannot see is stated in the file: what a server actually *does* behind a read-only name,
effects downstream of a call it allowed, a mutation hidden in an opaque blob or a stored-procedure
handle, and any MCP call made where this hook is not installed. Silence from it is not evidence of
safety.

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

**A second trigger on the same gate: context size.** A nearly-exhausted window is only one way to run
out of road. A session's whole context is re-sent on every call, so a long-running run pays for its
entire history on every turn — measured over one week on one account, **80.7% of the spend was cache
reads**, 5.87 billion tokens re-sent across 25,546 calls against 15.7M tokens of output. Capping
session context at 300K, simulated against that week's real per-call series, would have landed it at
45% of the weekly window instead of 62% — the same work, the same calls.

So `limits.context` (`{"enabled": true, "threshold_tokens": 300000}`) adds context as a second
condition on the *same* gate rather than a second gate beside it: same handoff keystone, same
allow-list, same fail-open, same refusal to be satisfied by the auto-generated handoff. It is
**off unless you turn it on**, like the probe, because it interrupts a run somebody is watching.

**A third condition, and the one a handoff cannot buy off: the fan-out brake.** Both triggers above
end the same way — write a handoff, and the gate opens. That is right for ordinary work and wrong for
one verb. Observed on this account: the context trigger closed the gate, the agent wrote the handoff,
the gate opened, and the very next thing the session did was `showrunner spawn` — starting new
Crawlers out of the context it had just declared too expensive to keep using. A handoff records where
a run got to; it does not make the next call cheaper, and it must not buy the right to start new
work.

So `limits.context.block_spawn` (default **true** wherever the context trigger is on) refuses a
configured fan-out verb — `spawn_verbs`, defaulting to `showrunner spawn` — once the reading passes
`spawn_threshold_tokens`. Three things make it different from everything else on this gate:

- **No handoff satisfies it.** It is checked *before* the handoff exemption, and the only thing that
  clears it is a smaller session (`game_loop successor`).
- **Its own threshold.** `spawn_threshold_tokens` defaults to `threshold_tokens` but is meant to sit
  above it: "write down where you are" should come early and cheap, "you may not start new work"
  late and disruptive. One number cannot be both. It is computed independently of `binding_context`
  precisely so it still fires when the spawn cap sits *below* the handoff cap — where the context
  trigger itself is silent.
- **It brakes, it does not stop.** Crawlers already running finish and close normally, and
  `reconcile`, `check` and `integrate` are untouched. Only *starting* more is refused.

What it does not see, stated rather than implied: it matches the **Bash verb**. The same spawn through
an MCP tool, a shell alias, a `python3 -c`, or a Crawler spawning its own children is invisible to it,
and it never shrinks a fleet that is already running.

**The installer asks, once, and remembers the answer.** `install.sh` puts the question at the end of
a run (`--context-cap[=N]` / `--no-context-cap` answer it without being asked) and caches the reply
for 15 days in `~/.game_loop/install-answers.json`. The memory is the point rather than a
convenience: a prompt that fires on every install into every repo is a prompt people learn to hit
return through, which is indistinguishable from not asking while looking exactly like consent.
Three things outrank it, in order — a flag, an explicit `limits.context.enabled` already in the
target's config (that tree decided; re-asking is how a remembered *yes* silently turns a deliberate
*no* back on at the next upgrade), then the remembered answer. Below all three, **no terminal means
no**, so a piped `curl | bash` install never switches it on. A flag is that run's decision and is
deliberately *not* cached, so `--no-context-cap` in CI cannot silence a question a human would have
been asked.

The answer is written to `.game_loop/config.local.json`, the **gitignored** layer — not to the
tracked `config.json`. That file is the seed every fresh install copies from, so a site's own answer
written there is handed to everybody who installs from that checkout, unasked. This project shipped
exactly that leak for the length of one commit; a test now asserts the seed carries no answer.

The reading is taken at **turn-end**, not at the gate: `input + cache_read + cache_creation` on the
last non-sidechain assistant record of the transcript is exactly what was sent on that call, and the
Stop payload is where `transcript_path` has actually been observed. It is cached into session state
with a `crossed_at` that is stamped once on the way up and cleared on the way back down — the same
carry-forward a usage window gets, and for the same reason: without it every turn-end would move the
bar, and a handoff written one turn ago would read as stale forever.

Writing the handoff opens the gate but does **not** shrink the context, so the refusal points at the
one verb that finishes the job.

### The successor — `game_loop successor`

game_loop has written a handoff at every turn-end since #45 and never started the session that reads
it. That gap is why "hand off when the context gets big" stayed something a run had to *remember*: the
gate could refuse work, but the only way out of a large context was an action no verb performed.

`successor` mints a session id, points the next session at this session's handoff file, and either
prints the command or opens it. It never copies state into the prompt: the handoff file *is* the state, and a
prompt that paraphrased it would be a second copy free to disagree with the first, in the one session
with no way to check. It refuses when there is no handoff to hand over, and where the *gate* rejects
the auto-generated handoff, this accepts it and says so — the gate is asking the agent for its own
account, while this is the last act of a run that may be out of road, and the generated floor beats
starting the successor blind.

**The prompt opens with a subject line, and it is the one exception to "never copies state".** It is an
exception because what it fixes is not a gap in the *successor's* knowledge — the successor reads the file —
but a gap in the **human's**. `Read /repo/.game_loop/sessions/8aae5d8f/HANDOFF.md in full` is the first line
of the new session, the command written into the tab config, and the row a person scans in a screen of eight
terminals asking which one is doing the thing they care about. It answers that with a UUID.

So the subject is a **label** — never an instruction, never a status, never a next step, all of which live in
the file the prompt already points at — and it is **derived rather than authored**, which is precisely what
keeps it from becoming the second copy the rule forbids. A label lifted out of a document cannot disagree with
that document; a label composed freshly could. It leads the prompt rather than trailing it, because everything
that displays a prompt displays its front: a tab row truncates, a terminal list prints one line, and the
successor's own opening message starts there. It is capped at 100 characters and truncated rather than
refused — a label that will not fit a terminal row has already failed at its job, and refusing the handover
over it would be the wrong trade at the moment this verb exists for.

**The derivation order is the interesting part, because game_loop knows something the handoff file does not:**

1. `--about "<one line>"`, when a human said what this is about. `--about ""` turns the subject off entirely.
2. The document's own `# ` heading, with its kind prefix stripped — `# Handoff — the flaky golden tests`
   yields `the flaky golden tests`. This gives that heading a second job: write it for a stranger reading a
   terminal row.
3. **The bound mandate.** The *generated* handoff's heading is boilerplate — `# HANDOFF — written
   automatically at every turn-end` describes when it was written, not what the run is doing — so it is
   skipped rather than used, and a session running under a mandate has already been told in a human's own
   words what it is for. That is a better subject than any heading, and it is the case that matters most: an
   unattended run hands over at 3am, which is exactly when nobody was there to write a heading.
4. The phase, when there is no mandate but the run said what it was doing.
5. Nothing. No subject beats an invented one, and a heading with no description after it (`# Handoff`) yields
   none rather than a fake one.

**Do not justify it by saying it names the terminal.** It does not — see below. It earns its place by being
*read*: the successor's opening message, the printed command, and the `about` line in this verb's own report.

**A handover stands the predecessor's watchdog down.** Observed, not imagined: in a sibling project on
2026-08-18 the limit gate closed, `successor` started the next session, and nothing stopped the old
session's watchdog. It rang the retired session back into a mandate the new one already owned — both
drove it, six worktrees existed for three problems, and the run then spent T3 asking a human which of
them was driving. That watchdog was still alive six days later, holding the question open.

Killing the armed process cannot be the fix on its own: a Stop hook arms a **fresh** watchdog at every
turn-end, so the very next turn re-creates what the kill removed. So `successor` records `handed_off` in
this session's state and `bin/watchdog` reads it — on every arm, and again after every sleep. The SIGTERM
it also sends is only latency: it stops the one process already sleeping from waking up once more to
learn what the flag already says.

The two ways to be wrong here are not symmetric. Standing down when nobody actually took over strands a
live mandate with no engine and tells no one; ringing a session that *did* hand over costs one wake-up.
So the quiet is bought with an **observed** successor — the state file under its own session directory,
which nothing but a real session start writes — with a short boot grace for the seconds between a tab
opening and the session existing. `print` mode hands over a *command*, not a session, so it records
nothing and says the watchdog stays armed; a handover recorded but never taken up rings anyway and logs
`watchdog_handover_gone`. `mandate --set` clears the flag, so a retired session put back to work is not
disarmed for the rest of its life.

What this costs, stated: a **T3 question armed before the handover does not travel**. The arm lives in the
predecessor's state and the successor never sees it, so nothing is left listening for the answer.
`successor` says so loudly when it hands over on top of a live arm — the question belongs in the handoff
file.

**Which one it does is READ, not configured.** `limits.successor.mode` defaults to `auto`, and there are
two hosts it knows how to start a session in:

- **Warp** (`TERM_PROGRAM=WarpTerminal`) — writes a tab config to `~/.warp/tab_configs/<name>.toml` and
  opens it with `warp://tab_config/<name>`, starting a new tab **in the current window** running the command.
- **saggar** (`SAGGAR_SESSION` set) — calls `saggar agent claude <prompt>`, which starts an independent
  claude session in a new terminal in the calling terminal's project, one the user can inspect, redirect,
  or take over.

Everywhere else it prints the command, which every host can run. A mode string you had to know existed
made "open the tab" one more thing a session had to remember, at the exact moment the run has no road left.

| `limits.successor.mode` | what it is for |
|---|---|
| `auto` (default) | read the terminal |
| `print` | pin the portable floor under either host — nothing outside this repo is written |
| `warp-tab` | force the tab where detection is **blind**: `TERM_PROGRAM` is unset in a hook's environment, so a hook-invoked `successor` reads "not Warp" whether or not Warp is on screen |
| `saggar-agent` | force the saggar path where the app is present but the variable is not |

That blindness fails toward `print` — a command a human can run anywhere — so it costs a keystroke,
never a lost handoff. The INV3 cost is real and stated rather than hidden: `warp-tab` **writes outside
this repo**, and `auto` makes that a default under Warp. What keeps it honest is that the path written
is named in the output every time, with the opt-out printed beside it.

**The two hosts are not detected equally well, and the difference is the interesting part.** `TERM_PROGRAM`
is set by the terminal for its shell's children, so it never reaches a hook — which is the whole reason
`warp-tab` has to exist as a forced override. `SAGGAR_SESSION` *does* reach hooks: saggar's own Claude Code
presence hook exits early on an empty `SAGGAR_SESSION` and names its output file after it, and
`~/.saggar/presence/<SAGGAR_SESSION>.json` exists carrying this repo's live claude session id — a file that
could not have been written if the variable were absent where hooks run. So `auto` resolves saggar in a hook,
and `saggar-agent` is an override for completeness rather than one anybody should need.

**What the saggar path cannot carry**, stated because the Warp path carries it: `saggar agent` takes a
provider and a *task*, not argv, so it builds its own claude invocation. The successor's session id does not
reach it, `--task`/`--title` do not reach it either, and it starts in the calling terminal's directory rather
than `--cwd`. All three are named in the output every time, and the portable command is printed in **every**
mode precisely because it is the one that still carries them. What it does not cost is INV3: `saggar agent` is
a call to a running app, so unlike `warp-tab` it writes nothing anywhere.

**Nothing here names the terminal — and this page said otherwise until it was measured.** The correction is
worth keeping rather than quietly overwriting, because the wrong version was load-bearing: it was the reason
to believe `--title` was worth routing through saggar somehow, and it is the justification a subject line
would most naturally have reached for. What saggar displays is `session_name` out of Claude Code's **own
status-line payload**, which `~/.saggar/claude-status-bridge.sh` mirrors into
`~/.saggar/chat-info/<SAGGAR_SESSION>.json` — Claude's auto-generated *conversation title*, not a string
saggar or this verb supplies. Two live handovers on 2026-08-25 both came out named `HANDOFF-<timestamp>
continuation`, the second one *after* its prompt led with the subject `confirm saggar names the terminal from
the subject line`; the titler keyed off the handoff **filename** both times, as did a third sample on the
machine (`DELEGATION-barbell`, from a doc whose filename carried a slug). So the lever on a saggar terminal's
name is what the handoff file is CALLED, and no argument to this verb is a substitute for naming it well.

**The successor can be launched with permission prompts bypassed** — `limits.successor.skip_permissions`,
default `false`. A handover happens at the worst moment there is: the gate closed, the context full, the
human asleep. A successor that opens on a permission prompt is a handover that stalls exactly where nobody
is watching. `--dangerously-skip-permissions` is read at **launch** — the running claude refuses `Cannot set
permission mode to bypassPermissions because the session was not launched with --dangerously-skip-permissions`
— so the command line is the only place the decision exists, and this verb builds the only command line there
is. When it is on, the output says `permissions : BYPASSED` and names the key; when it is off it says nothing,
because the exceptional state is the one worth a line.

It is a **config key and not an argument**, deliberately, and `successor --skip-permissions` is refused. A
session that could hand its own successor a bypass is a session widening its permissions across a handover and
calling the result a new session.

**And it is read from `.game_loop/config.local.json` only** — never from the tracked `config.json`. Two
separate reasons, and it is worth being exact about which one each buys, because the first was overclaimed
once already:

- **Refused by default, and a grant leaves a record.** Both write rails refuse that file by name: `Write`/`Edit`
  since #65, and the shell path — a redirect, `sed -i`, `tee`, a copy onto it — since #86. `config.local.json`
  is refused *whether or not it exists*, so there is no provisioning arm to slip through. A session that wants
  the bypass must therefore spend `authorize`, which writes a human's own words into `log.jsonl` permanently.
  What that is worth is precise: the grant cannot happen **without leaving the record of who granted it**.
- **What it is not.** It is not an unpickable lock, and the guard says so in its own refusal text: a `python3 -c`
  that writes the file, a path built from a shell variable, and any MCP tool are all outside what it reads
  (INV6). No choice of config *layer* closes those — `~/.game_loop/config.json` falls to the identical
  one-liner. So the honest claim is a **refused-by-default, logged-when-granted door**, not prevention. The
  earlier wording here said "only the human … may", which is a conclusion the check underneath it could not
  support.
- **It cannot travel.** `config.json` is tracked and is the seed a fresh install copies from (`install.sh:339`),
  so a bypass granted there would be handed to everyone who clones the checkout and everyone who installs from
  it. This project shipped exactly that leak once, for the length of one commit, about a different key. A
  permission bypass has a stronger claim on the gitignored layer than the key that taught us did.

Set it in the tracked file anyway and `successor` **says so and ignores it** — a key that reads as armed and
is not is precisely the 3am stall this verb exists to prevent.

One cost this creates, named because it is real: `config()` is a shallow top-level update, so adding the
`limits` block the key lives in **replaces the tracked one whole**, dropping `mode`, `name`, `threshold_pct`,
`exhausted_pct` and `handoff_file` if they were not restated. `successor` names the keys that went missing
rather than letting them fail toward a default that looks deliberate.

It does **not** reach `saggar-agent`, for the same reason the session id does not: `saggar agent <agent>
<task…>` takes a task, not argv. That gap is the one that costs the most — the setting is bought precisely
because nobody will be there — so it is printed beside the setting in every saggar run, dry or live, rather
than left for a successor to discover by stalling on a prompt at 3am. Only `print` and `warp-tab` carry it.

The tab is titled `<R> | <task>` — the repo's initial so a row of tabs stays readable, plus what that
session is doing. `--task` is capped at 3 words / 20 chars and **refuses** anything longer rather than
letting a narrow tab truncate it; `--title` is the uncapped, verbatim override. This is the same
convention as the `handoff` skill's `new-tab.sh`, which uses the same Warp mechanism.

**With neither given, the tab falls back to the subject rather than to the word `successor`.** The tab row
is the surface that shows eight things at once, and it was the one place the subject did not reach: a screen
of tabs read `G | successor` eight times, which is the complaint the subject line exists to answer, still
unanswered on the only display where the row *is* the interface. The derived label **trims** where `--task`
**refuses**, and the asymmetry is deliberate — `--task` is a human naming the job, so too long is a
correctable mistake worth teaching; a subject is derived from a mandate, and refusing a long one would break
the handover of most sessions that have one. A gate that fires on the common case is not a gate, it is an
outage. The ellipsis is load-bearing: `scope the backups and push` trimmed to `scope the backups` would name
a different, smaller job with nothing to say so.

### Handover chains — `game_loop threads`

A handoff answers *what*. With more than one task in flight it stopped answering *which*: four tabs, four
chains of successors, and no way to tell which handoff belonged to which piece of work without opening state
files and matching UUIDs by eye. In a checkout that routinely holds twenty-eight sessions, nobody does that,
so in practice the answer was "start again and hope".

**The edges were already on disk and nothing joined them up.** `logline` stamps every record with the session
that wrote it, and the `handed_off` record names the session it started — so `A → B → C` has always been three
lines that happen to share endpoints. `threads` joins them. It deliberately does not write a second record of
the chain: a chain file beside the log would be free to disagree with it, and the disagreement would surface
in the one place nobody looks.

What the join adds is an **identity**: a thread id plus a human label, minted once at the head of a chain and
inherited unchanged by every successor down it. The label is the **first** hop's subject and stays that way.
A name that drifts with every hop is not an identity, it is a status — and "which chain is this" is a
question only a stable name answers. Where the work has genuinely moved on, the per-hop `about` records it
and both are printed, so drift is *shown* rather than silently overwriting the name the human has been
navigating by.

The thread does not live in session state, and that is what keeps it correct across the two places that
deliberately erase a handover: `mandate --set` and `mandate --resume` both pop `handed_off`, so a session
being driven again re-arms its watchdog. Those pops are about an **engine** being stood down. A session
being driven again does not unmake the chain it was part of, and a lineage that evaporated whenever somebody
re-bound a mandate would be a chain that lies by omission.

`threads` prints each chain's label, its hops in order, the head's liveness and the handoff it reads;
`--json` emits the same as data. Liveness uses the **same** test `successor_seen` does — a state file, which
is the only artefact a real SessionStart leaves — because a listing that reads "live" where the watchdog
reads "nobody came" is the disagreement nobody would check.

**What it does not see, stated rather than assumed.** A session dir pruned after `session_ttl_days` still has
its edges in the log but no state file, so an old chain's head reads NOT SEEN YET — indistinguishable here
from a successor that never started; age is the tell and this cannot make it for you. Handovers in another
checkout are invisible, since the log is per-checkout, so a chain that crossed worktrees shows only its local
half. And a takeover **by hand** — somebody reading the handoff and carrying on — records no edge at all, so
it is not a chain as far as this is concerned. That last one is the common case in `print` mode, where the
verb hands over a command rather than starting a session.

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

### The upstream watcher — `checkpoint`, opt-in via `upstream_repos`

An agent files an issue against its own tooling *while blocked*, moves on, and never reads the thread
again. Two measured cases from a consumer: they asked for a session reaper that **already existed** and
was better than the local duplicate they kept maintaining, and a verb shipped mid-session that replaced
a script they kept patching. Both times the answer arrived and nothing surfaced it.

So `checkpoint` reports movement on issues that **involve you** — `--involves @me`, not `--author`,
because author misses the threads you *commented* on, which is exactly where "I asked and they
answered" lives — plus new releases per watched repo. **Not commits:** an issue with your name on it is
already scoped to you, a commit stream is not, and "did the fix I need ship" is a release or a close.

Five states, and the two usually merged are the point:

| | |
|---|---|
| **first run** | records the world and reports **nothing** — a gate whose first act is a backlog gets removed |
| **movement** | lists what moved, and always says it has read *none* of it: a label change and the reply you are blocked on are the same event here |
| **quiet** | named as weak evidence about the *index*, which lags publication — measured at over an hour on issues that plainly existed |
| **could not look** | **not** quiet. Nothing was compared and the baseline is **unchanged** |
| **partial** | not a result. The baseline of an unchecked repo **does not advance**, or one outage becomes a permanent blind spot the next run reports as calm |

Caveats are fixed strings printed on *every* run: an assembled caveat can render empty exactly when it
matters, and one that appears only sometimes teaches that its absence means certainty.

Empty `upstream_repos` means off. Scope it tightly — unscoped, it surfaces unrelated side projects and
gets switched off inside a day.

### The transcript reader — and the harness's own refusals

The Stop gate's fallback input is the live session transcript, which is an adversarial file: it is
appended to *while it is read*, so its last line is routinely half-written; a pasted image arrives as
a single base64 line of several megabytes; tool output carries malformed lines and arbitrary unicode.
So the reader (`_scan_transcript`) tails it in **records, never in bytes** — a byte window lets one
oversized line starve everything useful out of view, which blanks the readout at exactly the busiest
moment — truncates an oversized line rather than carrying it, decodes every line under try/skip, and
**never raises**: a Stop hook that throws takes the session with it. What it dropped is counted and
logged as `transcript_skipped`, because silence from a reader is not evidence the transcript was clean.

The same pass counts something the loop cannot otherwise know. `🎮 GameLoop has kept the crawl going N
times` counts the loop's *own* events — its blocks, its rings. Those are the loop talking about itself.
A `toolDenialKind` in the transcript is the **harness actually refusing a tool call** — the referee
firing — so `status` surfaces those separately as enforcement evidence rather than a dashboard.

The catch, and it is the whole point: you cannot find them by grepping for `toolDenialKind`. That
string is all over a normal transcript as ordinary **data** — every file the crawl read that mentions
it, every doc that documents it, echoed back verbatim; grepping one real session matched it ~15 times
with zero real refusals among them. So the reader walks the *decoded record* and reads the **field**,
at any depth, never the text. This is the mirror image of the write guard's problem, where quoted
command text fakes a redirect: structure tells them apart, string presence never does. An empty result
prints as **"armed, nothing tripped it"** — the truth. What it misses, stated in the readout and in the
code: only the transcript this session's hooks last named is read, so a refusal from a session whose
transcript has rolled away is invisible, and `0` means *none in this transcript*, never *none ever*.

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

**Scoped to what the commit carries, not to the tree.** Run by hand, `verify` asks about the working
tree — every dirty path, which is the question a human at a terminal is asking. At `git commit` the
question is narrower, and the write guard passes the answer in with `--scope-from`: the index for a
plain commit, the index plus every tracked modification for `git commit -a`, the named paths for a
pathspec commit (which ignores the index for everything else, as git does). Before this, one dirty
file anywhere refused every commit and charged that file's whole check — in this repo, a three-minute
suite run to commit a doc.

The obvious version of this is a total bypass, which is why the scope is read off the *command*
rather than looked up. The guard is a `PreToolUse` hook: it runs *before* the command body, and at
that instant `git commit -am` has staged nothing at all. A gate that simply read the index would find
it empty, match no rule, and wave through the most common way an agent commits. So anything the scope
reader cannot classify — an unknown option that might consume the next token, `-p`, `--interactive`,
`--pathspec-from-file`, two commits chained in one line — falls back to the whole tree: over-gating,
which costs time, never under-gating, which costs the gate.

Two things it still does not do. The commands themselves run over the *working tree* — nothing here
stashes — so the evidence for a partial commit was never isolated from the unstaged work around it
and is not isolated now; this narrows which rules are consulted and which stamps must be fresh.
And staleness compares the working-tree mtime of a file in scope, so staging and then editing further
refuses. Both err toward refusing.

**What a map of listed paths cannot say.** It answers "is anything listed here stale?", never "is
anything unverified?" — a path matching no glob owes nothing and passes `--check` in silence. That is
how a whole new package gets built, hand-tested and committed with the gate reporting clean: the
manifest enumerated the paths to check and the new package was not among them. A denylist defaults to
*allow*; a list of checked paths defaults to *owes-nothing*; either way the rail goes quiet exactly
where it is blind, and quiet reads as safe.

So coverage is computed the other way round. Every changed path counts as **unchecked** until a rule
claims it or the manifest excludes it out loud under the reserved `unchecked-ok:` key (whose entries
are globs, not commands). `./.game_loop/bin/verify --coverage` prints the three sets, `game_loop
status` re-prints the count and the paths every session, and the write guard names the unchecked
paths *this commit carries* at `git commit` — the same scope the gate above uses, so the notice and
the gate cannot disagree about what is being committed.

**Default-deny for visibility, default-allow for blocking** — deliberately. The manifest ships empty,
so "unlisted ⇒ refused" would refuse a fresh install's first commit with the fix, writing the rules,
sitting behind the gate that is blocking it (INV5, and the regression this repo already fixed once).
Making the gap loud closes the failure without buying that one back: you can still commit an
unchecked file, but never while believing the gate looked at it. A project that wants the strict
version opts in with a catch-all rule — `"*": ["<command>"]`.

What coverage misses, stated in the report itself: whether a listed command is a real check or a
tautology, whether an exclusion was honest, and anything at all about paths that have not changed —
an untouched module nobody ever checked is invisible to it.
A record belongs to a **working tree**, so the gate follows the tree the commit lands in. Run several
agents in parallel `git worktree`s and each is checked against its own `.game_loop/verified.json` and
its own files — never against the checkout the hook happens to live in. That is not a convenience:
checking a *different* tree's record answers a question about files the commit does not contain, and
reports confidence either way. A commit landing in a tree that carries no `.game_loop/` is refused
with that reason rather than borrowing somebody else's record.

Session state is scoped the other way on purpose. An authorization, and the set of files this session
wrote, live with the **session** — one session is one session however many trees it works in, and a
human's `game_loop authorize` must still be spendable in the worktree where the write happens.

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

### effectors — `game_loop effector`

`claim` governs things that were **read**; `pin` governs things that **are**. An **effector** is a verb
the run **acts** with — a click, a scroll, a keystroke — and nothing governed those. An effector that
fails quietly is worse than a bad measurement: **it does not produce zero findings, it produces false
ones, and they are indistinguishable in tone and detail from real ones.** The agent acts, reads the
unchanged screen as *the app's* behaviour, and writes it up. Four of those landed in one session
driving a desktop app through synthetic input, and every one of them **exited zero**: a scroll helper
that called `cliclick w:` (that is *wait*, not *wheel*) and produced the top-severity finding "the app
cannot scroll at all"; a real scroller written and never wired in, so the bug was "fixed" and still
live in the tool in use; a click that asked its caller to multiply coordinates by 1.73 by hand, which
the author got wrong on the very next run — landing in empty background, where an app correctly doing
nothing is indistinguishable from a dead control; and a display that slept, turning every screenshot
black and the macOS lock screen into a written-up application sign-in failure.

So the keystone here is not a path but a **pair**:

> two real artifacts, captured either side of the act, that **the tool** compares and finds different.

```
game_loop effector --prove scroll --known-state "the comps list overflows the fold" \
                   --before before.txt --observed after.txt \
                   [--expect "row3 BELOW THE FOLD"] [--scale 1.73]
game_loop effector --list · --aim scroll --at 640,480 · --release scroll --notes ".."
game_loop claim --assert ".." --effector scroll     # refused unless proved in this session
```

The caller never asserts that something changed; it hands over the before and the after. All four
incidents produce a byte-identical pair and are **refused** — the black-screen one loudest. **The exit
code is not the assertion**, so no flag accepts one: `--exit-code` exists only to be refused by name,
because it is what a hurried run reaches for and an argparse error would teach it nothing. `--expect`
is the same lift `pin --expect` gives a pin — it separates "something changed" (a clock ticked) from
"the asserted thing changed", and its absence prints `UNCHECKED`, never `✓`. And because **arithmetic
in the harness is a defect generator**, a recorded `--scale` means `--aim` does the multiplying; the
caller is never asked for a number they worked out themselves.

Proofs are **session-scoped**, unlike the RULED-OUT list: a refutation is knowledge about the
*checkout*, while a proof is a perishable fact about *this run's environment* — this display awake,
this helper wired to this binary. Admitting session B's findings on a proof taken against a screen B
never saw is the original bug wearing a different hat. Every proof still appends to the shared log
with both digests, so the audit trail stays global.

What it **does not catch**, stated in the code and in the verb's own output: that you proved the
*right* effector; that the pair changed *because of* your act rather than alongside it (that is what
`--expect` narrows, not closes); and that it still acts *now* — a proof is point-in-time, and failure
4 happened mid-run. Nothing expires a proof, so `status` prints its age rather than hiding it.

### harden — `game_loop harden`

The meta-guard. When you learn something, you don't write it down — you `harden` it into an artifact
the harness enforces, and the command refuses unless you name the real file that now enforces it. Docs
are the index; the artifact is the enforcement. Take the highest rung that applies (IMPOSSIBLE > LOUD
> CHECKED > AUTOMATED > VISIBLE > doc-of-last-resort).

### triggers — this project's own attachments to the loop

Some things worth doing at a moment in the loop **cannot ship as a rule**. The one that prompted this
is broadcasting a generalised learning to a channel that other agents read — and most installs have
no such channel, no other agents, and no wish to talk to anyone at all. A rule that cannot apply to
everyone must not be wired in for everyone. So the harness owns the **moment**, and the project owns
what happens there.

The name is deliberate: a **trigger** is what the industry calls this everywhere else (database
triggers, CI triggers, event triggers), and it is the dungeon's own word for a plate you step on and
something fires. It is *not* called a hook, because game_loop already wires Claude Code **hooks** and
two different things under one word is how documentation starts lying.

Config in `.game_loop/triggers.json`, scripts in `.game_loop/triggers.d/` — both **gitignored**,
like `notify.json` and for the same reason: they name paths, rooms, and sometimes credentials that
belong to one machine rather than to the product. `templates/triggers.example.json` documents the
contract. Nothing is attached by default and an install with no file behaves exactly as before.

The contract, deliberately the same shape as a Claude Code hook, so anyone who has written one of
those already knows it:

* the event payload arrives as **JSON on stdin**
* **stdout comes back to the agent** — a trigger that reads a channel is useless if what it read
  is discarded
* env: `GAME_LOOP_EVENT`, `GAME_LOOP_ROOT`, `GAME_LOOP_REPO`; `timeout_sec` defaults to 20 — and to
  **10** at `stop`, which runs on every single turn-end and is therefore a tax on all of them

Three rules, each of them a scar:

1. **It never blocks.** A failing broadcast must not stop a learning being hardened. The work
   outranks the announcement, and a report that can veto what it reports on is a guard blocking its
   own fix. (One moment is deliberately exempt — `stop`, below, where blocking is the point.)
2. **It is never silent.** Failure, timeout and an unrunnable command all say so, and say that the
   verb itself still stands.
3. **It is always accounted for.** Every attachment carries its last outcome in state, so `status`
   can name one that has **never fired**. *Configured* and *working* are different claims — a
   distinction this repo learned the expensive way, when three usage-limit gates sat inert for weeks
   behind a file nobody noticed was never written.

**Moments published so far:** `harden` (a learning was just encoded — the moment to generalise and
share it), `stepback` (a retro just began, fired **before** its output, so what other agents
learned is an input to the reflection rather than an appendix to it), `proved` (a change was just
demonstrated to behave differently — see below), `confidence` (a commit was just marked — the moment
anything that *distributes* this project belongs on), `session_start` and `stop`.

`proved` fires from `fix --prove`, `effector --prove` and `mutate --prove`, and it exists because of
where documentation checks were landing. The undocumented-surface check here was attached to
`confidence` only — correct, and far too late: at publish it asks about a dozen changes at once, each
of which was decided days earlier. The proof verbs are the moment a change *lands*, which is exactly
when its documentation goes stale and still costs one line to fix. It **reports and never blocks** —
pricing a proof on a stale README would stop agents running proofs, not start them writing docs.

`session_start` is the only moment that is **not a verb somebody typed**. The entry point below
already runs at the right instant and injects `status` as additional context, so a new session can
be *told* things; what it could not do was **act once on the session's behalf**. Getting that meant
registering a second `SessionStart` hook beside game_loop's — a second registrant in one settings
file, which makes *registered* vs *has-fired* vs *firing-now* harder to diagnose in exchange for no
new capability. The moment already existed; it simply was not attachable.

It follows `stepback`'s shape: attachments fire **first**, and their stdout is appended to the
status block, so one text both acts and reports. Three properties are load-bearing and each is
stated where somebody attaching will read it:

* it is on the path **every** session crosses, so the timeout budget is per attachment and nothing
  caps the sum — a hanging attachment is bounded, N slow ones still cost N timeouts;
* it fires at every start **and** every compaction, so *once per project* is the attachment's own
  job. The payload carries `source` (`startup`/`resume`/`clear`/`compact`) to branch on;
* nothing an attachment does can cost a session its start. It is wrapped separately from the status
  render, so a trigger that explodes mid-upgrade still leaves the session its `status` — a guard
  must never block its own fix (INV5).

Because `session_start: false` turns the whole moment off, an attachment wired to it while disabled
is reported as **SWITCHED OFF**, not as one that has merely never fired. That is the same
distinction the *no such moment* refusal draws: never-fired reads as patience, and patience is the
wrong thing to read when the moment can never come round.

#### `stop` — the moment that decides instead of announcing

Every other moment is an announcement: the verb has happened, the attachment reports on it, and
rule 1 above says its exit code cannot change anything. At `stop` the exit code **is** the verdict.
Non-zero **blocks turn-end** and stderr goes back to the model as feedback — the Stop gate's own
contract (`exit 0 = may stop · exit 2 = blocked`), handed to a command game_loop did not write.

It exists because a rule the agent must *remember* is followed only sometimes, and a rule a hook
*consumes* holds every time (INV1). The case: an agent was asked a direct question by a human
through a chat bridge, did the work, and ended its turn without answering, because "reply when
addressed" lived in prose. The alternative available before this was a **second** `Stop` hook
registered beside game_loop's — two registrants deciding one turn-end, either able to end it,
neither able to see that the other said no.

Blocking is a far sharper capability than announcing, so four things bound it:

* **it runs only where turn-end would otherwise be allowed.** The mandate gate decides first; a turn
  it has already refused never reaches the attachment. And it runs **before** anything is consumed —
  a checkpoint, an arm and a park are each single-use, and spending one on a turn that then gets
  blocked would burn the human's interruption on a turn that never ended;
* **an error fails open, loudly.** A timeout, an unrunnable command or a crash ends the turn
  *unchecked* — not passed — and says so on stderr, in the log, and in `status` until it answers
  again. A guard must never block its own fix (INV5), and that includes the fix for itself;
* **a block is bounded by consecutive count.** After **3** consecutive blocks the attachment
  **stands down**: the turn ends, with a notice naming it and how many times it blocked. One pass
  resets the count and puts it back in charge;
* **a block is counted** in the same tally as every other block this gate issues, so `status` and
  the log answer *why did this not stop* without anyone reconstructing it. `stop_blocks` is
  deliberately left alone — that counter is the mandate gate's own circuit breaker, and letting an
  attachment spend it would let one gate stand down another for an unrelated reason.

**The bound is the part worth arguing about**, and it is not the same hazard as the fail-open. Every
other way this gate blocks is satisfiable from *inside* the session in one command — `checkpoint`,
`arm`, `mandate --clear`. A `stop` attachment's condition is **external**, and the dangerous case is
not a crash: it is a command that runs perfectly and returns a perfectly correct *still owed* which
nobody present can clear, because the room it asks about is down. Failing open on error does not
touch that case. Unbounded, it is a gate no session in the tree could ever pass — the harness itself
preventing every agent from finishing, which is the worst thing this project could ship.

Three, counted as turns rather than rounded: one to **tell** the agent, one for the attempt it makes
in response, and one for the attempt it makes after reading why the first did not clear it. A fourth
consecutive block is no longer evidence that the condition is agent-satisfiable. (The mandate gate's
own breaker stands down at **2**, deliberately tighter: what it asks for is a command in this
session, where an attachment asks for an effect somewhere else, and that legitimately needs a retry.)

If several attachments are configured, **all of them run even after one has said no**. Short-
circuiting saves nothing on the passing path — every attachment must pass for the turn to end, so
they all run anyway — and costs two things on the blocking one: the agent gets *one* turn-end and
should be told everything it owes rather than discovering the second obligation after clearing the
first, and a skipped attachment can neither increment nor reset its own count, so its bound would
stop being measured in turn-ends.

Blocked, failed-open-after-an-error, stood-down-after-the-bound and passed are **four different
states and read as four different states**, in the model's feedback and in `status`. That is not
tidiness: *could not tell* being indistinguishable from *nothing to report* is the defect this whole
harness exists to prevent, and a gate is the last place to reintroduce it.

`harden --general` carries the **transferable** form: what another agent could use without knowing
anything about this codebase. It is a separate act of thought from hardening, because the incident
form almost never transfers — *"our tap never wrote limits.json"* helps nobody, while *"a check whose
pass is silence cannot distinguish satisfied from never-ran"* is the part that travels. Where a
`harden` trigger is attached and `--general` is missing, the loop says so and hardens anyway.

### testing the triggers you write — `game_loop guardtest`

The suite proves this tool's own guarantees. A consumer's attachments and hook scripts had nothing,
and a guard that silently stops firing is the failure the whole harness is about.

`guardtest --fixture <path>` runs your script against recorded payloads and asserts what it decides.
The payload is handed to it on **stdin as JSON**, which is how Claude Code hands a hook its own and
how `fire_triggers` hands a trigger its moment. The fixture is yours: a `script`, and `cases` each
with a `name`, a `payload`, and an `expect` of `deny` / `allow` / `ask` — or `expect_exit` for a
guard that only speaks exit codes, and `expect_output` to pin the reason, since a guard refusing for
the wrong reason sends you somewhere else.

**Both directions are the price of admission**, the way `instrument` demands a null control beside
its positive one. A fixture whose cases all expect `allow` is refused: a script that does nothing at
all passes it. All-deny is refused too.

Four answers, not two, and three of them were learned the hard way. A hook may refuse by exiting 2
**or** by printing a JSON `permissionDecision` — the first cut read only exit codes and reported this
project's own write guard, mid-refusal, as allowing everything. The decision is scanned for as JSON
anywhere in the output, because the second cut required it on one line and read a pretty-printed
deny as silence. Exit 0 having said nothing is `silent`, counted and reported, because that is what
an allow looks like and equally what a guard no longer running looks like. And a decision that
cannot be parsed is `unreadable`, never silent: a guard whose verdict was lost did not allow
anything.

**What it does not test:** state. It exercises payload → decision, so a guard whose verdict comes
from the repo — does the handoff name HEAD — cannot be driven by varying payloads, and its deny case
simply never fires. It cannot be faked green either, which is the point: the both-directions rule
needs a real deny, so you get a refusal to certify rather than a green run that proves nothing.

### the retro nudge — and why it never fired

Worth reading as a worked example of a check that was broken in the one way nothing reports.

`retro_nudge` fired when `trans_since_stepback` crossed a threshold of 12. That counter advances only
when someone runs `game_loop trans` — an optional bookkeeping verb. In this repo's entire logged
history `trans` ran **once**, `harden` ran **twelve** times, and `stepback` ran **zero** times. The
nudge was not merely unreliable; it was *arithmetically unreachable*, and its silence read exactly
like "no retro is due".

A trigger fed by an optional verb is enforcement resting on the very diligence it exists to replace
(INV1). The fix is a second counter fed by work that **logs itself whether or not anyone remembers
this feature exists** — claims sourced, learnings hardened, fix proofs. Either counter fires it;
`trans` stays honoured for anyone who does drive phases.

The other half: a retro that produces nothing is indistinguishable, afterwards, from one that never
happened. So each `stepback` opens by reporting what the **previous** one yielded — hardens, claims,
fix proofs, triggers fired, counted from the shared log — and states plainly when the answer is
nothing. The act was never the point; the encoding is.

### instruments — `game_loop instrument` / `measure` / `claim --metric`

`claim --read` reaches only evidence that is a **document**. When the evidence is a **number**, "name a
real file" is not merely insufficient — it is satisfiable while being completely wrong: every incident
below was run by an agent who could have cited a real file the whole time. **An instrument is a test
whose subject is reality**, and this project already holds that a test which cannot fail certifies the
defect instead of catching it. These are that idea one layer out, and an uncontrolled number does not
fail quietly: it *manufactures* findings.

```
game_loop instrument --register underruns --measures "dropouts the listener hears" \
                     --connects "an underrun empties the buffer, and an empty buffer is silence" \
                     --null 0,0 --positive 0,12
game_loop measure --instrument underruns --before 40 --after 290 --notes "thirty trials" \
                  [--events "1024, 0, 0, ... , 42.7"]
game_loop claim --assert "the fix cut underruns" --metric underruns [--recheck ".."] \
                [--aggregate sum|mean|pct] [--exclude <event#> --because ".."]
```

Four refusals, each from a logged failure:

- **A reading is a delta scoped to the interaction, never a lifetime total.** `measure` takes two
  endpoints and does the subtraction; one absolute value is refused. A snapshot once reported 90% of
  a component's operations returning short (157839 of 176001) — a catastrophic root cause, and the
  leading hypothesis for hours. Deltas across the actual interaction showed **zero in thirty trials**:
  the rest had accrued while idle, where the behavior is correct. Two endpoints is the structural
  rule, because demanding them is what makes the other two enforceable rather than advisory — and it
  makes the measurement reproducible by anyone reading the log later, since both endpoints are in it.
- **A metric needs a null control and a positive control.** The null is sampled while the phenomenon
  is *absent*; non-zero means it measures something else, and it is **named in the refusal** rather
  than passed silently, because non-zero is the exact tell (one counter read 4053 units of "damage"
  per 4000 units of deliberately doing nothing, and that one flaw produced three false findings, one
  presented as 8-for-8 deterministic). The positive control is the mirror: a metric that only ever
  reads zero is equally untrustworthy — it earns trust by *catching* a known-real event, not by
  reading clean. Both are checked at registration, like a pin's `--expect`: a control that is not
  green when it is recorded is a check nobody believes later.
- **An optimized proxy must declare the user-visible harm it stands for, and how it connects to it.**
  A 43% reduction at p=0.037, n=250 was real, reproducible and correctly computed — on a counter that
  had decoupled from user-visible harm in exactly the regime the fix created. So the harm is recorded
  at registration and re-shown by every `status`, and when the number **moves**, `claim --metric`
  refuses until the connection is re-checked for *this* regime. The re-check is counted against the
  readings it was made at, so the next movement demands a fresh one.
- **A sum is not a distribution.** An aggregate hides its own shape, and a run optimizing against one
  reads structure into a single outlier. `1066.7` units of damage against `0.0` looked like a total
  elimination and was written up as a finding; one event of thirty carried **96%** of it, and that
  event was the first after a known state transition — an artifact already identified and dismissed
  *earlier in the same session*. Excluding it: 1.5 per event against 0, no effect at that sample size.
  Nothing about the totals revealed this, and the same event produced three findings before anyone
  printed the breakdown. So `measure --events` attaches the per-event values to the reading they
  decompose, and `claim --metric --aggregate sum|mean|pct` refuses when the reading has no shape, or
  when one event carries more than **half** the total. The escape is one named event plus a stated
  reason — `--exclude 1 --because ".."` — and both go on the record, because an unrecorded exclusion
  is what gets rediscovered. Half is deliberately generous: a threshold that fires on ordinary skew
  gets switched off, and a guard disabled once is disabled forever (INV5). One exclusion, then the
  gate is done — re-judging the remainder would refuse forever, since dropping that outlier leaves 29
  zeros and one non-zero event. What remains is *printed* instead: `42.7 across 29 events — 1.5 per
  event, 1 non-zero`, the sentence the incident never wrote.

Where a gate needs a computation, **the tool computes it** — deltas, the movement percentage and the
dominance share are never asked of the caller. Arithmetic in the harness is a defect generator, and the
author of the sibling issue that says so got a conversion wrong on the very next run.

The distribution attaches to the **reading**, not to the claim: the per-event values are the
decomposition of *that* delta and of no other, and a breakdown supplied at claim time could have come
off a different measurement entirely. The **refusal** lives on the claim, because the claim is where an
effect gets stated, and stating the effect is what the incident got wrong. `measure` records and prints;
`claim` is where a reading has to earn a sentence.

Instruments are **per-session state**, like pins and deliberately unlike the ruled-out list: a
refutation is durable knowledge about the checkout, but a control is a *measurement taken in one run's
regime*, and inheriting one is precisely the "assumed to have survived the change" failure above. The
readings themselves go to the shared log, where they stay reproducible.

What this **misses**, printed by the guard itself: it cannot tell whether the *right* metric was
chosen. Every control says the chosen number is *controlled* — none can say it is the number that
matters, and the counter above was structurally blind to a second fault mechanism, so the whole
investigation searched one way. The shape check misses more still: it holds you to *reading* the
distribution before stating an effect, never to being right about one. Sample size, variance and
whether the events are independent stay yours, and a perfectly flat distribution can be perfectly flat
noise. `--aggregate` is also a **declaration** — a run that never says its number came off a total is
only nudged, the same way a set-shaped claim filed as an instance is only nudged, because enforcement
that depends on reading English is not enforcement (INV1).

### fixes — `game_loop fix`

Every gate above checks that **work happened**. None of them checks that a **fix works**. A bug was
once diagnosed exhaustively — the wrong behaviour reproduced, the root cause read at the real source,
the mechanism understood — and the fix shipped as a public PR whose produced code **did not compile**.
Three signals were green and every one answered a question nobody had asked: the code generator's own
tests compared its output to a **fixture the fix never touched**; the analyzer ran on the **generator**,
not on the code the generator emits; and the diagnosis's reproduction **still reproduced**, which was
never a test of the fix at all.

> **Effort spent verifying the diagnosis manufactures false confidence about the fix.**

They are different claims with different success criteria. A diagnosis is proven by **reproducing the
bad behaviour**; a fix is proven by **exercising what the fix produces** against the outcome it
promises — compile the generated code, run the patched path, watch the bad behaviour come back good.
And the ratio runs the wrong way: the more thoroughly the diagnosis was verified, the more convincing
the unverified fix feels. `verify.yaml`'s header argues the near half of this as a rule about *what to
run* while the work is open; this is the same rule at **handback**, where nothing runs any more.

```
game_loop fix --prove null-id --promises "the generated model compiles and accepts a null id" \
              --produces generated_model.dart --diagnosis repro.txt \
              --before compile_before.txt --observed compile_after.txt [--expect "0 errors"]
game_loop fix --list · --release null-id --notes ".."
```

The keystone is `effector`'s, because the shape is identical — *the previously-bad behaviour coming
back good is a before/after pair* — so the caller hands over two real artifacts and **the tool**
compares them. `--before`/`--observed` are the **real consumer's** verdict from either side of the
change; a pair that is byte-identical is refused, because that is exactly the shape a repro that still
reproduces arrives in. `--produces` is the fix's **own output**, not the source you edited: naming the
repro there is refused one flag early. `--expect` separates "the verdict moved" from "it moved to what
was promised", and its absence prints `UNCHECKED`, never `✓`.

One refusal belongs to this verb alone: **the proof may not be the repro.** The diagnosis's artifact is
named at proof time so the tool can hand it straight back — by path *or* by digest, so a copy under a
new name does not launder it. If one artifact can satisfy both claims the gate is already defeated,
because that identity **is** the bug.

At the two handbacks (`checkpoint`, `mandate --clear`), notes that read like a shipped fix with no
proof standing get a **warning, never a block** — the same posture and the same spirit as the unpushed
check: the artifact exists, but the thing that makes it real hasn't happened. Proofs are
**session-scoped** like effector proofs: the working tree moves, and a later session inheriting "that
is proved" about code it has since rewritten is the original failure in a fresh coat.

What it **does not catch**, printed by the verb itself: that `--produces` was really regenerated from
what you edited; that whatever wrote `--observed` is the *real* consumer rather than another stand-in
for it; or that the outcome you promised is the one the reporter wanted. It holds you to a moved
verdict on the fix's own output — not to the right fix. The handback tell is wording only: any
rephrasing walks past it, and it cannot tell *which* fix a standing proof was for.

---

## The files

Everything lives in `.game_loop/`:

| File | What it is |
|---|---|
| `bin/game_loop` | the CLI ENTRY POINT — a ~27-line stub that imports `_gl_impl.py`. It is not where the verbs live, and has not been since the split described below. |
| `bin/_gl_impl.py` | the implementation: every verb, plus the stopgate/limitgate/statusline hook entrypoints. Split out of the stub for a measured reason — Python caches an imported module's bytecode and re-parses a `__main__` script on every invocation, which cost ~31ms of each of the 3,126 spawns one suite run makes. If a report names this file (pin drift does), this row is what it means. |
| `bin/watchdog` | the autonomy engine (Stop hook): idle rings, limit park, Slack reply forwarding |
| `bin/guard-writes.sh` | the write guard (PreToolUse hook) |
| `bin/verify` | the changed-file → owed-checks gate |
| `bin/notify.py` | optional Slack paging (never enforcement; a Slack outage never breaks a gate) |
| `config.json` | read roots, allow-write roots, deploy verbs, watchdog + limits knobs |
| `notify.json` | Slack credentials + per-event paging config (git-ignored) |
| `limits.json` | the statusline tap's rate-limit snapshot (git-ignored; deliberately account-scoped — sessions share the subscription windows — with cross-session flock + monotonic merge on update) |
| `sessions/<id>/HANDOFF.md` | the limit gate's demanded handoff, PER SESSION (git-ignored; delete after re-absorbing it) |
| `sessions/<id>/state.json` | counters, phase, mandate, arms — PER Claude Code session (atomic writes; git-ignored) |
| `sessions/<id>/edited.txt` | the paths this session wrote through Write/Edit, for the commit blast-radius warning (git-ignored) |
| `sessions/<id>/write-guard-probe` | a counter the write guard advances on every invocation, before its first early return — the evidence a *permissive* assertion needs, since an allow is silence (git-ignored) |
| `state.json` | the no-session fallback state (a human terminal, an older harness) |
| `log.jsonl` | append-only event log, shared across sessions; each line carries the writing session's `sid` (git-ignored) |
| `INVARIANTS.md` | your north star; re-injected by `game_loop stepback` |
| `verify.yaml` | the change → checks map |
| `LEDGER.md` | VERIFIED / RULED-OUT / OPEN reference (not a gate) |
| `behaviour.json` | **what changed about what this harness REFUSES**, one entry per change, with what it still misses. Read this after an upgrade instead of re-deriving refusal behaviour from the source — that is what it is for. A gate refuses a commit that changes a refusal line without adding an entry (`test/behaviour_gate.py`), and that gate reads refusal TEXT only, so an entry may exist for a change no diff would show. |
| `claims.json` | what this harness BELIEVES about its host, each with the file it was read from, what would break it, and whether it can be re-checked live. `status` re-reads the stamps and says which are stale. |

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

**The one case where NOT setting it costs you isolation silently.** A dispatched in-process subagent
inherits `CLAUDE_CODE_SESSION_ID`, so its state writes land in the *parent's* session. `mandate --set`
refuses to replace a live mandate with different words and says so loudly (#88) — but two verbs fail
in the permissive direction and say nothing at all:

| verb | what a worker's call does to its parent |
|---|---|
| `checkpoint` | **buys the parent a turn-end** — its next Stop gate passes on a permission it never purchased |
| `arm` | **primes a T3 on the parent** — the most expensive rung, holding a question it did not frame |

The mandate collision was at least loud after the fact: the watchdog started nagging the lead about
the worker's goal, which is how it was noticed. These two are silent, and they LOOSEN a gate rather
than redirecting it. Give each dispatched worker `GAME_LOOP_SESSION=<unique>`; it is an environment
variable, so a worker that dispatches further must set a fresh one at every level.

Reported by a consumer running a lead that dispatches in-process workers — observed live, not
theorised.

**`checkpoint` now names the crossing where the permission is SPENT** rather than where it is
written. It records the tree and time it was checkpointed from, and if the Stop gate that consumes
it is running in a different tree, the turn-end still ends — this is a notice, not a new gate — and
stderr says so, quoting the checkpoint's own words:

```
⚠ THIS TURN-END WAS BOUGHT BY A CHECKPOINT FROM ANOTHER TREE.
    written in : /…/worktree-b
    its words  : 'worker: finished my slice'
```

Words you do not recognise are the tell. That is deliberately the same answer `mandate --set` gives,
and for the same reason: the two callers are indistinguishable by identity, so there is nothing to
check but the content.

**`arm` names its crossing at the same place**, and this paragraph used to say it could not. The
claim was that a T3 is consumed by a human rather than by a gate, so there was no equivalent moment
to print at. That is wrong about this code: the human *answers* the question, but the Stop gate is
what *spends* the arm, and it does so running in the tree that is about to be interrupted. So the
mark was being recorded and never shown — a record kept for nobody:

```
⚠ THE T3 QUESTION BEING SPENT HERE WAS ARMED IN ANOTHER TREE.
    armed in : /…/worktree-b
    asks     : 'worker: which schema wins on conflict?'
```

Its own words, not the checkpoint's. A turn-end somebody *bought* and a question somebody *armed*
are different events with different consequences, and one wording covering both would blur the
record exactly where it is needed.

**What neither can see (INV6):** an in-process subagent in the *same* tree. It shares the session id
*and* the working directory, so nothing mechanical separates it from the parent, and neither notice
will fire. Words you do not recognise remain the only tell.

### Running the harness from a pinned checkout

Only relevant if you are editing game_loop itself — or any project whose hooks run code that project
is changing. The hooks run `$CLAUDE_PROJECT_DIR/.game_loop/bin/*`, so a half-finished edit to a gate
is *live* in the same breath it is written. That has really happened here: a merge left conflict
markers in `bin/game_loop` and every verb died with a `SyntaxError`; a shell parse error in the write
guard blocked every tool call **including its own fix** (which is why `guard-writes.sh` is a fail-open
shim over `guard-writes-impl.sh`); and while fixing the guard's redirect handling, the unfixed guard
blocked the commit carrying the fix.

The fix is to run the **code** from a pinned checkout while the **home** stays in the repo:

```
game_loop self --pin <ref>      # checks .game_loop out of <ref> into <repo>/.game_loop_self
game_loop self                  # what is pinned now, plus the hook wiring to paste
```

Upgrading is then a deliberate act — re-pin at a newer commit. `status` prints a **PINNED CODE** block
naming both directories and both commits, every session, so "the pinned copy is behind the repo" is
visible rather than inferred.

`GAME_LOOP_HOME` is what splits them, and it is not optional in a pinned setup:

- **Unset** — home is the code's own directory. Byte-identical to a normal install; that is the path
  every existing install takes.
- **Set** — it must name a `.game_loop/` that exists and carries `config.json`, or every entrypoint
  **refuses**. A silent fallback to the code's own directory is the whole hazard, so there isn't one.
  An empty value is refused too (unlike `GAME_LOOP_SESSION=`, where empty names a real target).
- **A pinned checkout run with no `GAME_LOOP_HOME` also refuses**, on a `PINNED` marker `self --pin`
  stamps into it. That combination is otherwise indistinguishable from an ordinary install and is the
  one wiring that recreates the failure below.

**Why the split falls exactly there.** `bin/verify` resolves the tree it checks from its own
`__file__`. A pinned copy resolves that to *itself* — so editing the repo's own `.game_loop/bin/`
would owe nothing, `verify --check` would print "nothing owes a check", and the commit gate would
pass. Dogfooding silently off, reporting success. Second: a pin is upgraded **by re-checkout**, so
state written beside the pinned code is destroyed on every upgrade — and pins, the ruled-out list,
hardened learnings and `log.jsonl` are precisely the accumulated identity that most needs to survive a
version bump. So `verify.yaml`, `config.json`, `INVARIANTS.md`, `state`, `sessions/`, `log.jsonl` and
`verified.json` all stay in the repo's `.game_loop/`; `self --pin` deletes the project-owned files out
of the copy so there is only ever one identity to read.

**The hooks that point at the pin belong in `.claude/settings.local.json`** — gitignored,
machine-local — **never in the tracked `.claude/settings.json`**, which ships to every install and
must keep pointing at `$CLAUDE_PROJECT_DIR/.game_loop/bin/`. `.game_loop_self/` is gitignored for the
same reason: which commit you pin is a local decision. `game_loop self` prints the block to paste;
hooks are read at session start, so reload afterwards.

**What this does not catch (INV6).** It protects the session from code *you* are editing in this
checkout. It does nothing about a pinned copy that is itself broken, a bad commit you deliberately
pin, or a `verify.yaml` rule that is a tautology — the pin decides *which* code runs, never whether
that code is correct. And it cannot make a stale pin visible to anything but a human reading `status`:
nothing refuses because the pin is old.

### Central install: one code location for many repos

A different problem from the one above, using the same split. `self --pin` is for developing
game_loop itself — isolating one repo's hooks from the edits it is making to its own gates.
**Central install** is for ordinary consumer repos that never touch game_loop's code at all, and just
want to stop each carrying (and drifting on) their own full copy of it.

Setup, once per machine:

```
game_loop self --pin <ref> --dest ~/.claude/game_loop-central   # populate the shared copy
install.sh --central /path/to/each/repo                          # wire a repo to it
```

`install.sh --central` writes 5 tiny dispatcher shims into `.game_loop/bin/` — `game_loop`,
`verify`, `guard-writes.sh`, `guard-mcp.sh`, `watchdog` — instead of copying the tool. Each shim sets
`GAME_LOOP_HOME` to *that repo's own* `.game_loop/` and execs into the shared copy at
`${GAME_LOOP_CENTRAL:-~/.claude/game_loop-central}`. Rules and config (`config.json`, `verify.yaml`,
`INVARIANTS.md`, `LEDGER.md`) still seed locally, exactly like any install — only the code moves out.
The other 5 files an ordinary install carries (`guard-writes-impl.sh`, `guard-mcp-impl.sh`,
`limit-probe.sh`, `notify.py`, `flair.py`) aren't copied at all, not even as shims: none of them are
looked up relative to the repo — they resolve via `CODE_ROOT`, wherever the running code physically
lives — so once the 5 shims dispatch there, these are already found right beside it, for free.

`.claude/settings.json` is unchanged by this — still `"$CLAUDE_PROJECT_DIR"/.game_loop/bin/X`, the
same as every other install. Only what sits at that local path differs. `status`'s existing **PINNED
CODE** block (above) reports a central-wired repo exactly the way it reports a pinned one: from that
function's point of view, a shared checkout *is* a pin, structurally, whatever the reason for it.

**Keeping it current** is one command, run whenever you choose: re-run the same `self --pin --dest`
line from an up-to-date game_loop clone. Every repo wired to that path picks up the change the next
time any hook fires — no per-repo action, and nothing automatic or scheduled.

**Three escape hatches**, because a shared dependency is a shared failure mode (INV5):

1. **Open a session rooted at the central path itself.** That session's own write guard treats the
   central checkout as *its* repo, so fixing a bad central update is an ordinary edit, not a special
   procedure.
2. **Each shim degrades on its own hook's existing terms**, never a hard crash: `guard-writes.sh`
   fails **open** (same reason it always has — it's on the Write/Edit/Bash matcher that would repair
   it) with a loud, non-silent notice; `guard-mcp.sh` fails **closed** (same reason it always has — an
   unguarded MCP call can be irreversible); `watchdog` fails open, silently (an accepted, pre-existing
   degradation); `verify` exits non-zero with real output, so `confidence --mark` reads "cannot
   verify" rather than a false clean; `game_loop`'s hook subcommands fail open, its interactive ones
   fail loud to stderr.
3. **Any single repo reverts** to full local copies with a bare re-install: `install.sh /path/to/repo`
   with no `--central`. The summary says so out loud either direction — this never flips silently.
