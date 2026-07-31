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
else — other projects, your home directory, system files — is read-only by default. It covers
`Write`/`Edit`/`NotebookEdit` and Bash mutators (`rm`, `mv`, redirects, `git` writes, `sed -i`, …),
resolving paths with realpath and tracking `cd` across a command. It also blocks configured
deploy/publish verbs anywhere. It states what it does *not* catch (interpreter one-liners, paths built
from shell variables) right in the file — a guard that overstates its reach is worse than one that
states its limits.

The only way past it is the human, single-use and logged: `game_loop authorize --path <prefix> --reason
"<their words>"`.

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
the system enforces, and the command refuses unless you name the real file that now enforces it. Docs
are the index; the artifact is the enforcement. Take the highest rung that applies (IMPOSSIBLE > LOUD
> CHECKED > AUTOMATED > VISIBLE > doc-of-last-resort).

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
| `sessions/<id>/edited.txt` | the paths this session wrote through Write/Edit, for the commit blast-radius warning (git-ignored) |
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
