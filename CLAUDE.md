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

## Where things live

| Topic | Source of truth |
|---|---|
| How the guardrails work | `docs/how-it-works.md` |
| North star + non-negotiables | `.game_loop/INVARIANTS.md` |
| What a change owes | `.game_loop/verify.yaml` |
| The guarantees, as tests | `test/run.py` |
| Installing into another project | `install.sh`, `README.md` |
