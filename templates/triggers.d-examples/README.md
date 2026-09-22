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

## WHAT A BLOCKING TRIGGER MAY SELECT ON — `@me` is an ACCOUNT, not a session

Reported on game_loop#130, from a real block. A gate selected the work it holds you to with
`gh pr list --author "@me"`. On a machine where several agent sessions share one GitHub login —
the normal case — `@me` does not identify a session. It identifies the **account**. So the gate
saw every session's open PRs as its own and fired on whichever session happened to be ending a
turn.

Two sessions, agreed separate lanes. Session A opened a PR. The gate fired on **session B's**
turn-end, naming session A's PR. The only remedy it offered was `touch <marker>`, and that marker
means *"I wrote the description."* Session B could not satisfy it honestly, and refused — the
right call, and one made under turn-end blocking pressure.

**The rule, and the reason it is about blocking specifically:**

> A trigger that **BLOCKS** must select on something *this session* owns.
> A trigger that only **NOTIFIES** may select on the account.

Widening is harmless for a notice — an issue involving the account is worth knowing about
whoever touched it. For a block it is not a nuisance, it is corrosive: it routes an obligation to
the one party structurally unable to discharge it honestly, and the remedy on offer means "I did
the work". The gate's whole integrity rests on that marker being written by whoever wrote the
thing; account-scoped selection works directly against it.

**What IS session-scoped and available today:** local git state — the current branch, this
worktree, unpushed commits, files this session wrote. `example-unpushed-at-stop.sh` blocks, and
selects entirely on `git rev-list @{u}..HEAD`, which cannot see another session's work.

**What is NOT available, said plainly so nobody builds on it:** game_loop does **not** record
which session opened which pull request. There is no PR-to-session mapping anywhere in the tool,
so "scope it by `GAME_LOOP_SESSION`" is not buildable for PRs today, however reasonable it sounds.

**And when a blocking gate genuinely cannot tell:** say whose work it is and tell the agent to
notify that author. Never offer a remedy whose meaning is "I did the work" to a session that did
not do it. That keeps the rule and removes the invitation to lie, and it needs no new tracking.

**`example-own-branch-prs.sh` is the mechanism, not just the rule.** For a while this section said
only the rule, which is how #130 happened in the first place: a consumer reached for the obvious
selector because no correct one was shipped. That example asks the tracker the session-scoped
question directly — `gh pr list --head "$(git rev-parse --abbrev-ref HEAD)"` — instead of listing
the account's PRs and filtering afterwards. Its firing and quiet fixtures differ **only** by which
branch the checkout is standing on, which is the property being claimed.

What it still cannot do, and says so in its own header: two sessions on the **same branch** of the
same checkout are indistinguishable to it and to everything else game_loop has. It narrows "the
account" to "this branch". It does not reach "this session", and a gate built on it must not claim
to — which is why it still tells you not to touch the marker if the work is not yours.

## The example gates

- **`example-harden-without-claim.sh`** (`stop`) — reads a synthetic `log.jsonl`. Tests it by
  writing lines directly to `$GAME_LOOP_ROOT/log.jsonl` in a throwaway directory (fixture shape 1).
- **`example-own-branch-prs.sh`** (`stop`) — the #130 shape: selects by the branch this checkout
  is on, never by the account. Tested by moving one throwaway repo between branches with `gh`
  stubbed (fixture shape 3 + 2), so the firing and quiet cases differ only in that one property.
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
