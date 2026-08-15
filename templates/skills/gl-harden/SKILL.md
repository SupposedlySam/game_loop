---
name: gl-harden
description: Turn a learning into something the harness enforces instead of something a session has to remember — pick the highest rung of the harden ladder that applies, write the artifact first, record it with `game_loop harden`, and add the verify.yaml rule that makes a regression fail. Use after a bug, a near-miss, a retro, a code review finding, or whenever the user says "make sure that never happens again", "remember to always X", "add that to CLAUDE.md", "gl-harden", "harden this", or a fix lands whose lesson would otherwise live only in prose.
---

# gl-harden

**A rule an agent must remember is followed only some of the time.** Long sessions and compaction
break promises; a check the harness consumes holds every time. So "write it in CLAUDE.md" is the
*last* rung, not the first, and taking it without saying why 1–5 don't apply is how a project ends up
with a document nobody's behaviour depends on.

The keystone is the same as the claim gate: **the artifact must be a real file that already exists.**
Not a plan to write one. `harden` resolves every `--artifact` path and refuses the ones that aren't
real.

## Step 1 — Name the learning in one line

The transferable form, not the incident. "Our tap never wrote limits.json" helps nobody; "a check
whose PASS is silence cannot tell *satisfied* from *never ran*" is the part that travels to another
project. Both are worth recording — the second goes in `--general`.

## Step 2 — Take the highest rung that applies

```
1 IMPOSSIBLE — change the code/design so the mistake CANNOT be made (the rule stops existing)
2 LOUD       — assert/guard AT the point of misuse (fails in 1s, not 3h later, with the reason)
3 CHECKED    — a build/CI/test check that fails on regression
4 AUTOMATED  — the tool just does it (no step left to remember)
5 VISIBLE    — the harness REPORTS the fact, so it's read, never guessed
6 (doc/memo) — LAST resort only; you must say why 1-5 genuinely don't apply
```

Work **down** from 1, and stop at the first rung that can actually carry this learning. The usual
honest landing spot is 2 or 3. Reaching for 6 first is the failure this ladder exists to catch.

A test that says "the rule holds" is rung 3 and it is worth writing even when the rule also lives in
prose — then the prose is the **index** and the check is the **rule**.

## Step 3 — Write the artifact, then run it and watch it FAIL

A check that has never failed is not known to be a check. Before recording anything: break the thing
deliberately, confirm the new assert/test/guard goes red, put it back, confirm green. A rule with no
observed failure behind it is the one shape this project refuses to add — the invariant is *no gate
without a logged, observed failure*.

And the guard must never block its own fix. If the check you are adding would refuse the commit that
repairs the thing it guards, it is the wrong check.

## Step 4 — Wire what a change OWES, if the artifact is a test

`verify.yaml` maps globs to commands, and **a path no glob matches owes nothing and passes in
silence** — which is how a whole package gets built, hand-tested and committed while the gate reports
clean. If your learning was "this file needed checking", the rule is the hardening:

```yaml
"<glob>":
  - "<command that FAILS when the change is broken>"
```

A command that cannot fail is not a check. Then confirm the coverage report agrees with you:

```bash
./.game_loop/bin/verify --coverage
```

Excluding something is a legitimate answer too — `unchecked-ok` keeps that decision visible and
counted, rather than silent.

## Step 5 — Record it

```bash
./.game_loop/bin/game_loop harden \
  --learning  "<the one-line rule you'd otherwise have to remember>" \
  --artifact  <real path[,path] to the code/assert/check/tool that enforces it> \
  --mechanism "<how it now fails loudly, or becomes impossible>" \
  --rung      <1..6> \
  --general   "<the form another agent could use knowing nothing about this codebase>"
```

`--learning`, `--artifact` and `--mechanism` are required. `--general` is optional and is the part
that outlives this repo — if the project has a harden trigger attached, `harden` will say so when it
is missing, and will still record the learning either way.

## What this is not

- **Not a place to log that something happened.** `note` is for that. `harden` claims a learning is
  now enforced, and a recorded hardening whose artifact does nothing is worse than no record — it
  reads, afterwards, exactly like a rule that holds.
- **Not a substitute for the fix.** Prove the fix separately (`fix --prove`, with the fix's own
  output — a repro is refused back as proof). Hardening stops the *next* one.
- **Not automatic generalisation.** Generalising is a separate act of thought from hardening, and the
  incident form rarely transfers.

## Never

- Never record a `--artifact` that does not exist yet. Write it first.
- Never take rung 6 without stating why 1–5 genuinely don't apply — in the record, where it can be
  read later.
- Never add a gate you have not watched fail.
- Never harden a rule by deleting the check that caught the problem.
