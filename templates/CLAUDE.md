# <project> — instructions

This project runs under **bumper** (guardrails for unattended Claude Code sessions). This file is an
**index, not a reference**: if you catch yourself explaining a mechanism here, it belongs in a doc —
and if you catch yourself *asking* someone to remember a rule, it belongs in a guard.

## Run this first, every session and after any compaction

```
./.bumper/bin/bumper status
```

Rehydrates from disk: the cost ladder, the invariants one-liner, claim/harden counters, the current
phase, and whether a mandate is bound. **Surviving compaction is the point.**

## The one hard rule

Before you assert anything about external reality — a dependency, a harness, another repo — name the
real file you read:

```
bumper claim --assert "<what you're about to say>" --read <path> --confidence "<what would refute it>"
```

Prose is what an LLM produces fluently and forgets completely. **"Name a file that exists" is the only
check prose cannot satisfy.**

## Everything outside this repo is READ-ONLY

Read other projects, mine patterns, use their data as fixtures. **Never write, never run their
tooling, never deploy.** Access is not permission. Enforced by `.bumper/bin/guard-writes.sh`, not by
this paragraph.

## Working unattended

Under a mandate (`bumper mandate --set "..."`) the Stop gate won't let you end a turn by asking a
question or by claiming you're "continuing" and then stopping. Keep working, or:

- `bumper checkpoint --notes ".."` — report progress and hand back (no question)
- `bumper arm --question .. --read .. --predict ..` — ask something you genuinely can't derive
- `bumper mandate --clear --notes ".."` — the work is actually done

## Where things live

| Topic | Source of truth |
|---|---|
| North star + non-negotiables | `.bumper/INVARIANTS.md` |
| VERIFIED / RULED-OUT / OPEN findings | `.bumper/LEDGER.md` |
| What checks a change owes | `.bumper/verify.yaml` |
| How the guardrails work | `docs/how-it-works.md` (in the bumper_bot repo) |
