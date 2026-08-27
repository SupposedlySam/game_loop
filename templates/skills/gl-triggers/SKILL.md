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

## Four ways a two-direction test still cannot fail

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

The tell they share: in all four the assertion was GREEN while its subject was BROKEN, and green is
what you were hoping for. A check you have never seen fail is a check you have not tested — and
knowing that rule does not exempt the check you just wrote *because* of it.

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

## And three ways a MEASUREMENT cannot be wrong

The same defect one level up. An assertion that cannot fail and a measurement that cannot be wrong
are the same thing wearing different clothes — both are green, both feel like evidence, and neither
was ever at risk of disagreeing with you. All three of these produced a confident number that was
simply not the quantity anybody wanted.

**Cross-check a derived reading against something you observed independently.** A per-producer
column read as durations summed to 378 minutes of work — in a run that had visibly taken 70. One
division against a fact already in hand. If a reading has no independent check available, say so
when you report it.

**Measure the same quantity, the same way, on both sides.** Three ad-hoc comparisons of two runs
gave 2.2x, then "3.0x declining to 1.75x", then a decline that did not exist — one of them
comparing A-excluding-setup against B-including-setup. Completions per minute at matched fractions
settled it in a single step. Pick the quantity first, then take it identically from both.

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
