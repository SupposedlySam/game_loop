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
