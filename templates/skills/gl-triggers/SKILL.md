---
name: gl-triggers
description: Test a trigger or hook you wrote, in both directions, against a fixture — so "this guard still fires on the case that created it" is a runnable check rather than a memory. Use when adding or changing anything in .game_loop/triggers.d/, when a guard stops firing, when a check needs a positive control, or when the user says "test this trigger", "does this hook still work", "gl-triggers", or "why didn't that fire".
---

# gl-triggers

A trigger that has stopped firing looks exactly like a trigger with nothing to report. Both exit 0
and say nothing. So the question "does this still work?" cannot be answered by running it once and
seeing silence — silence is what both answers look like.

`game_loop kinds` and the dead-kind check catch a trigger matching a `kind` string nothing emits.
They cannot catch one that names a REAL kind and gets the condition wrong. That needs the trigger
actually run, against a fixture, in both directions.

## The two properties, and why the second is not optional

**Every trigger needs a firing case AND a quiet case.** Not the important ones. Every one you write.
A trigger with only the case it was built for has never been shown to leave correct behaviour alone.

**Weight them differently, because the failure costs are not symmetric.** A false quiet costs one
missed catch — bounded, and it fails in the direction the gate already tolerates on every other
turn. A false FIRING costs the gate entirely: a check that blocks legitimate work is one a person
routes around within a day, and it stays routed around long after the bug is fixed. Write more quiet
cases than firing ones.

## Four fixture shapes that cover most triggers

1. **Synthetic log** — write lines directly to `$GAME_LOOP_ROOT/log.jsonl` in a throwaway directory.
   For anything reading the event log. Get the `kind` and field names from `game_loop kinds`, which
   extracts them from the source, rather than guessing.
2. **Throwaway git repo** — `git init` a temp dir, make the state you need, point the trigger at it.
   For anything reading git.
3. **Stubbed external command** — put a fake binary earlier on `PATH`, or use whatever env var the
   trigger already honours. For anything shelling out to `gh`, `curl`, another CLI.
4. **Recorded payload on stdin** — hooks receive JSON on stdin. Capture a real one once, replay it.

## Assert against the REAL output, never a description

Compare the trigger's actual stdout, stderr and exit code. A fixture that asserts "it should say
something about staleness" passes on a trigger that says the wrong thing about staleness.

```bash
out=$(GAME_LOOP_ROOT="$tmp/.game_loop" GAME_LOOP_REPO="$tmp" bash my-trigger.sh </dev/null); rc=$?
[ "$rc" = 1 ] || { echo "FIRING case did not exit 1"; exit 1; }
case "$out" in *"the phrase a reader acts on"*) ;; *) echo "fired without saying why"; exit 1;; esac
```

## Three outcomes, not two

A trigger has a third state: **it could not tell.** The log was unreadable, git could not answer, the
external command was missing. That is not "nothing to report", and giving it the same exit code as
"all clear" is how a broken check reads as a healthy one. Give it a distinct exit and say which.

## When your discriminator cannot prove identity, report what it CAN establish

A trigger that asks "is this mine?" usually has no way to prove it. Mine cannot: two agents post to
the same tracker through one account, so the login proves nothing and only a sign-off in the body
is reliable — and only in one direction. A comment carrying it is certainly mine; one without it is
merely *not provably* mine.

Choose the failure direction deliberately: report something already handled rather than silently
drop a real one. Then say the rest out loud instead of collapsing it. "This is from your own
account but unsigned — probably yours, not proof" sends the reader somewhere; "1 reply waiting"
sends them to re-read a comment they wrote.

Live cost of getting this wrong: every comment I posted with a CLI that does not append the
sign-off came back as work owed to me. I wrote myself a note to use the other tool and broke it with
the next command — which is why the discriminator belongs in the report and not in anybody's memory.

## The trap worth naming

**A fixture written by the author of the bug encodes the bug.** The trigger and its test, written
from the same wrong mental model, agree with each other and both disagree with reality — a green run
in that state is not a second opinion, it is the same opinion twice. This is why the fixture asserts
against real output, and why the quiet case matters: it is the half your mental model is least
likely to have covered.

## Worked examples

`templates/triggers.d-examples/` in the game_loop source carries three triggers with their fixtures,
and `test/trigger_fixtures.py` exercises them in all four shapes above. Read those before writing
your own — they are the same shapes, already debugged.

## Five ways a two-direction test still cannot fail

Writing a positive case and a negative case is not the same as having a check that can fail. Each of
these was written by someone who believed they had both directions, and each was found the same way:
**break the subject on purpose and watch THIS assertion go red** — not the suite, this assertion.

**The observable does not discriminate.** A refusal and a mismatch both exited 3, and both printed
the case name and the bad value. Every clause in the assertion held whether the thing under test
worked or not. Ask what the *broken* run would print, not what the working one does.

**An escape that matches nothing.** `"\\u2713"` written inside a raw string compares against a
literal backslash-u-2-7-1-3, which appears in no output ever. The assertion read as a glyph check
and was a check for a string that cannot occur. If a test is looking for a character, print what it
is actually comparing.

**The fixture sits where the subject is exempt.** A write guard allows `/tmp`, `/private/tmp` and
`/var/folders` outright — so a fixture built with `mktemp` comes back ALLOWED no matter which tree
the guard thinks it is guarding. The assertion measured the exemption, not the behaviour. Look for
an observable that survives it: that guard's refusal *names the repo it is protecting*, and that
still discriminates.

**The check runs in a scope nobody selects.** Assertions placed above a section header join the
previous section. They run in a full suite and are invisible to `--section`, so a targeted rule that
claims to cover them covers nothing.

**The stub stands in for one of two seams.** A test replaced the runner a tool calls, and the tool
was refactored to call a second entry point beside it for the extra data it now needed. The stub
still existed, still matched its old signature, still got patched in — and every call went to the
REAL runner instead. Nothing raised. The test kept returning a verdict, but about a run nobody in
the test controlled, and the only visible symptom was that the section went from milliseconds to
minutes. **A slowdown is what a bypassed stub looks like**; I read that as machine load and said so
out loud before checking. Assert the stub was REACHED — count its calls and fail if the count is
zero — because "my double was used" is exactly the thing a refactor can quietly stop being true.

The tell they share: in all five the assertion was GREEN while its subject was BROKEN, and green is
what you were hoping for. A check you have never seen fail is a check you have not tested — and
knowing that rule does not exempt the check you just wrote *because* of it.

## When you probe a guard, VARY THE INVOCATION — your habit may be the hole

I spent an evening concluding a commit gate was broken. It was not. Every probe I wrote ended
`2>&1 | head -N`, because that is how I capture output in order to read it — and `2>&1` is exactly
what makes that gate not fire. **I tested it three times, over an hour, exclusively through the one
form that escapes it, and never once ran the bare command.**

The bill: six theories, all mine, all dead — the gate's own directory, the pinned code, a temp
file's key, the inherited environment, the harness's permission mode, and finally "it is local to
this checkout". That last one I stated confidently on the strength of two other repos failing to
reproduce it, and it sent three of us looking in the wrong place for an hour. **They had run the
BARE form.** Their gates worked because of how they invoked, not because of what they had.

What would have caught it on the first probe, cheaply:

- **Run the plainest possible form once.** Not as the experiment — as the control. If the bare
  command behaves differently from your convenient one, the difference IS the finding.
- **Vary one token of the invocation at a time.** `cmd`, `cmd | x`, `cmd 2>&1`, `cmd 2>&1 | x`. That
  table took four minutes and localised what an hour of theorising could not.
- **Notice when every reproduction shares an accident.** Mine all shared a redirection I had never
  thought about, because it was in my fingers rather than in my hypothesis.

The general form: **a guard reads the caller's command TEXT, so the shape of your probe is part of
the experiment, not part of the plumbing.** An agent writes commands in a habitual house style —
piped, redirected, chained — and a habit applied to every probe is a constant, not a control. The
measured facts survived all six rounds here; every inference layered on them died, and the one that
survived came from changing the invocation rather than the reasoning.

## The most expensive shape: a true sentence promoted to a blocker

Three times in one session I reported something as impossible or forbidden, repeated it for hours,
and was wrong — and every time the sentence I started from was TRUE.

- *"A wake requested and never delivered is invisible from in here."* True of ONE wake: the run that
  would record it is the run that did not happen. Not true of a declared CADENCE — a path claiming
  every 10 minutes with nothing landed in 700 is a dead path, visible from inside. The issue had
  been open on exactly that.
- *"verify.yaml is a policy edit the guard refuses by design."* True of `INVARIANTS.md`, which
  denies. Not true of `verify.yaml`, which was allowed on a standing grant. I had assumed two files
  in one list shared a rule.
- *"The no-git fallback covers a tarball install."* True that such a tree has no `.git`. Not true
  that the install path exercises it — it ships no test suite at all.

The shape is always the same: a correct observation about **one case**, generalized into a claim
about the general case, and never tested in the general form. It survives because re-reading it
confirms it — the sentence really is true, so checking your reasoning cannot catch it. Only checking
the WORLD can.

Two questions that would have caught all three:

**"True of what, exactly?"** Name the case the sentence is true of. If the blocker is broader than
that case, you have not tested the blocker.

**"What would I run to see it?"** A blocker that cannot be reduced to a command is a belief. Each of
these took one: declare a cadence and backdate a timestamp; feed the guard a payload; look inside a
finished install.

A blocker is a claim like any other, and the expensive ones are the claims nobody asks you to prove
because they only ever stop work rather than starting it.

## Before you add a report, check the tool is not already printing it

Three times in one day I began building a report that already existed, and each time the fact was
in output I had asked for and not finished reading.

- A sweep verdict named twelve assertions as outside its denominator and said nothing about why.
  I diagnosed the cause, wrote it up, and started on a patch to print it. The sweep prints it —
  three lines below where my `head -14` stopped, in more detail than my patch would have.
- I argued that a re-pin of the running harness ought to be recorded somewhere a session could
  read it. `status` had been printing the pinned-commit-versus-HEAD delta at every session start
  the whole time. The gap was that I had not read it.
- A guard refused a write "by design", or so a note of mine said; a later note of mine said the
  opposite. Reading the guard settled it in one grep, and the file had not changed in between.

The pull is the same each time: a truncating pipe turns a report into an excerpt, and the excerpt
gets reasoned about as if it were the whole. `head`, `tail`, `grep -m1`, "first 20 lines" — all
fine for looking, all dangerous the moment a conclusion rests on the absence of something.

**The check is one command.** Before proposing that a tool SHOULD say X, run it and search its
whole output for X:

```
<the tool> <the args> 2>&1 | grep -i "<the thing you think is missing>"
```

If that comes back empty, you have a finding. If it does not, you have saved yourself the feature.
Absence in an excerpt is not absence.

The tell is a sentence in your own draft beginning *"it does not say"* or *"nothing reports"*.
That sentence is a claim about the complete output of a program, and you can only have it by
reading the complete output — which is cheaper than the feature you were about to write.

## Auditing a guard: follow the caller, not the guard

The sharpest miss of the day was not an instrument, it was a QUESTION.

A write-guard function's test looked too weak — it counted items processed, which a trimmed run
satisfies just as well as a full one. Its own docstring warned about exactly the failure that
weakness would allow. That felt like confirmation, so the search stopped: finding intent and test
diverged *inside* the function was mistaken for finding the behaviour unprotected.

The protection was one frame up, at the call site, in a plain `if not FAST:` around both writes.

**"This function's check is insufficient" is not "this behaviour is unprotected"** until you have
followed the call. And a plausible mechanism is the most expensive thing to be wrong about, because
a mechanism ends the search — you stop looking once you can explain it.

What settled it was the measurement the claim had predicted: the artifacts came back byte-identical
with unchanged mtimes. **If a claim about behaviour can be measured, measure it before reporting
it** — the read is a hypothesis, and this one had already survived being written down, drafted a
fix, and been reported three times.

## And five ways a MEASUREMENT cannot be wrong

The same defect one level up. An assertion that cannot fail and a measurement that cannot be wrong
are the same thing wearing different clothes — both are green, both feel like evidence, and neither
was ever at risk of disagreeing with you. Every one of these produced a confident number that was
simply not the quantity anybody wanted — and the count in this sentence said THREE for as long
as there were five, which is the smallest instance of it on the page.

**Cross-check a derived reading against something you observed independently.** A per-producer
column read as durations summed to 378 minutes of work — in a run that had visibly taken 70. One
division against a fact already in hand. If a reading has no independent check available, say so
when you report it.

**Measure the same quantity, the same way, on both sides.** Three ad-hoc comparisons of two runs
gave 2.2x, then "3.0x declining to 1.75x", then a decline that did not exist — one of them
comparing A-excluding-setup against B-including-setup. Completions per minute at matched fractions
settled it in a single step. Pick the quantity first, then take it identically from both.

**Know what your score structurally cannot count — a CORRECT measurement can still rank the wrong
thing first.** Mutation testing neuters a producer to its *nothing* value, so every assertion
covering the nothing answer survives by construction: it cannot flip, because the mutant returns
exactly what that assertion expects. The kill count therefore measures only the something-direction
assertions, and the tool's headline advice — strengthen the lowest scores first — points hardest at
whichever producers have the best-tested negative direction. Measured across 132 producers: all
twelve of the lowest-ranked neuter to their own nothing-answer, and the one at the very bottom had
four assertions across three directions. Nothing was wrong with the number. It answered a narrower
question than the ranking implied, and only reading the assertions showed which. When a score drives
a work queue, write down what it cannot see before you work the queue.

**Check the THING, not a proxy that usually travels with it.** Five times in one day my own
verification matched something adjacent to its subject. Greps for a verdict word (`BELOW FLOOR`,
`NOT MEASURED`) matched assertion *names* that quote the tool's vocabulary — in a repo whose tests
are written in that vocabulary, the words cannot separate a verdict from prose about verdicts; the
column layout can. A `diff` of section headings, run to see whether an edit had lost content, was
order-sensitive in a file whose order I had just changed, so it reported everything moved as gone. A
set comparison of the same headings then reported restored content as missing, because I had renamed
it. Only comparing SENTENCES found the one thing actually dropped. Elsewhere the same day, someone
compared two set sizes, found 19 and 19, and reported that the sets partitioned a tree identically —
equal cardinality, different members.

The tell is that the proxy is easier to extract than the subject: names are greppable and content is
not, sizes are one call and membership is a loop. **Ask what the check would say if the subject were
wrong but the proxy unchanged** — and when an edit is what you are auditing, never verify it with a
property the edit changed.

**Before controlling for a cause, verify the EFFECT is real.** A hypothesis about why a rate dropped
got a proper control — which tested whether the drop had that cause, and never whether the rate had
dropped at all. It had not. A control for a phantom looks exactly like rigour, and reads like it in
a commit message.

## The harness, for the shape it fits

This skill used to close by saying the tooling did not exist and this was the method. That stopped
being true — `guardtest` ships it:

```
game_loop guardtest --fixture <path>
```

The fixture is yours and stays yours: a `script`, and `cases` each with a `name`, a `payload` handed
to the script on stdin, and an `expect` of `deny` / `allow` / `ask` (or `expect_exit` for a guard
that only speaks exit codes, and `expect_output` to pin the REASON). It enforces the rule this skill
argues for — **a fixture whose cases all point one way is refused**, because a script that does
nothing passes it.

It reads a refusal by `exit 2` and by a JSON `permissionDecision` alike, reports `silent` for exit 0
with nothing said, and `unreadable` for a decision it cannot parse. Each of those is a separate
answer on purpose: two of them are what a guard that stopped running looks like.

## What it still does NOT give you

**It tests payload → decision, and that is not every guard.** Measured against a real trigger whose
verdict comes from repo STATE — does the handoff name HEAD — where varying the payload varied
nothing and the deny case simply never fired. Nothing was wrong with the trigger; the harness did
not fit it.

It cannot be faked green either, and that is the point: the both-directions rule needs a real DENY,
and a script whose answer does not depend on the payload cannot produce one. So you get a refusal to
certify rather than a green run that proves nothing.

If your guard reads state, the thing to vary is the state. That is a different harness, and it does
not exist yet.
