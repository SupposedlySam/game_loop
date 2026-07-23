# bumper_bot — instructions

This repo **is** bumper, and it **dogfoods** bumper (the payload lives in `.bumper/`). This file is an
index, not a reference.

## Run this first, every session and after any compaction

```
./.bumper/bin/bumper status
```

## The one hard rule

Before asserting anything about external reality — a dependency, a harness, another repo — name the
real file you read: `bumper claim --assert ".." --read <path>`. "Name a file that exists" is the one
check prose cannot satisfy.

## When you change a gate, re-run its test

The gates are the product. A change to `.bumper/bin/*` owes `python3 test/run.py` (wired in
`.bumper/verify.yaml`), so `git commit` refuses until the test has run since the change. This is the
tool holding itself to its own rule: checked, not remembered.

## Where things live

| Topic | Source of truth |
|---|---|
| How the guardrails work | `docs/how-it-works.md` |
| North star + non-negotiables | `.bumper/INVARIANTS.md` |
| What a change owes | `.bumper/verify.yaml` |
| The guarantees, as tests | `test/run.py` |
| Installing into another project | `install.sh`, `README.md` |
