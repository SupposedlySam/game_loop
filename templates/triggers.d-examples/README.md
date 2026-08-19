# Testing your own `triggers.d` scripts

`game_loop kinds` and the dead-kind check in `status` (#87) catch a trigger that names a schema
that does not exist. They cannot catch a trigger that names a *real* schema and still gets the
condition wrong, or one whose fixture agrees with its own bug. That needs the trigger actually run,
against a fixture, in both directions — the same way `test/trigger_fixtures.py` exercises the two
example gates in this directory.

## The two properties worth carrying into your own suite

**Every trigger needs a firing case *and* a quiet case, with no exemptions.** Not "the important
ones," not "the ones I'm unsure about" — every one you write. A trigger that only has a case for
the condition it was built to catch has never been shown to leave correct behaviour alone.

**Weight the two directions differently, because their failure costs are not symmetric.** A false
quiet costs one missed catch — annoying, bounded, and it fails in the direction the gate already
tolerates on every other turn. A false firing costs the gate *entirely*: a check that blocks
legitimate work is a check an agent (or a human) routes around within a day, and once that happens
it stays disabled long after the false positive is fixed. Write more quiet cases than firing ones
for exactly this reason — a gate earns trust by being right about *not* speaking far more often
than it needs to speak at all.

The lesson underneath both: **a fixture written by the author of the bug encodes the bug.** A
trigger and its test, written from the same wrong mental model, agree with each other and both
disagree with reality — a green suite in that state is not a second opinion, it is the same opinion
twice. That is why the fixtures here assert against the *real* trigger's real stdout/stderr/exit
code, never against a description of what it's supposed to do, and why the log-based fixture below
checks its own `kind` values against `game_loop kinds` rather than a hand-maintained list.

## The example gates

- **`example-harden-without-claim.sh`** (`stop`) — reads a synthetic `log.jsonl`. Tests it by
  writing lines directly to `$GAME_LOOP_ROOT/log.jsonl` in a throwaway directory (fixture shape 1).
- **`example-unpushed-at-stop.sh`** (`stop`) — reads real git state. Tests it against a throwaway
  git repository, with and without a configured upstream (fixture shape 2).

`test/trigger_fixtures.py` also exercises the shipped `example-answer-owed` gate from
`templates/triggers.example.json` by stubbing the external command it shells out to on `PATH`
(fixture shape 3 — the same idea as pointing `gh` at a fake binary via `GH_BIN`, generalised to
whatever tool your own trigger calls), and ships a fourth fixture (a repo with an `origin/main` and
a feature branch diffed against it) for gates that read `git diff` against a base ref — provided as
infrastructure only, since nothing shipped here needs it yet.

Copy either script as a starting point for your own project's `triggers.d/`, and copy the fixture
shape that matches what it reads, not the specific gate.
