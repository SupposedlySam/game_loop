# game_loop — instructions

This repo **is** game_loop, and it **dogfoods** game_loop (the payload lives in `.game_loop/`). This file is an
index, not a reference.

## Run this first, every session and after any compaction

```
./.game_loop/bin/game_loop status
```

## The one hard rule

Before asserting anything about external reality — a dependency, a harness, another repo — name the
real file you read: `game_loop claim --assert ".." --read <path>`. "Name a file that exists" is the one
check prose cannot satisfy.

## When you change a gate, re-run its test

The gates are the product. A change to `.game_loop/bin/*` owes `python3 test/run.py` (wired in
`.game_loop/verify.yaml`), so `git commit` refuses until the test has run since the change. This is the
tool holding itself to its own rule: checked, not remembered.

## The house voice — one banned word

**Never write "system" (or "systems") in this repo.** <!-- theme-word-ok --> The theme is
Dungeon-Crawler-Carl: the AI is the **Crawler**, and the thing enforcing the rules is the
**harness**, the **loop**, or the **gate** — never that word. Fun flair lives ONLY in
`.game_loop/bin/flair.py`.

This lived in a gitignored scratch file until #33, which is to say it did not exist: it reached
nobody who cloned the repo, and the tool's own `harden` success line broke it — the verb whose
entire purpose is converting a learning into something enforced rather than remembered, announcing
that in a sentence the house rule forbids.

**A test enforces it** (`test/run.py`), so this paragraph is the index and the test is the rule —
which is the whole of INV1. The check is word-boundary, so `SystemExit` and `filesystem` are
different words and pass untouched. If a line genuinely needs the word, mark it
`theme-word-ok` and the scan will skip it; that marker is greppable, so the exceptions stay
countable instead of accumulating quietly.

## Where things live

| Topic | Source of truth |
|---|---|
| How the guardrails work | `docs/how-it-works.md` |
| North star + non-negotiables | `.game_loop/INVARIANTS.md` |
| What a change owes | `.game_loop/verify.yaml` |
| The guarantees, as tests | `test/run.py` |
| Installing into another project | `install.sh`, `README.md` |
