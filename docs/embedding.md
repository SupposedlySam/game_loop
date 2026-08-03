# Embedding game_loop in another tool

game_loop is designed to be installed *by* something else — an orchestrator, a template, a bootstrap
script — as well as by a human. This page is the contract for that caller. It exists because the
first tool to do it seriously had to re-derive all of it by reading source, and then hardcoded a list
of internal filenames that would rot silently the moment this repo added one.

If you find yourself modelling something in here, that is the signal: **it is a missing verb, not
your job to reimplement.** File it.

## What game_loop owns

- **The `PreToolUse` and `Stop` hooks**, and their registration. `install.sh` *merges* game_loop's
  hooks into `.claude/settings.json`; it does not overwrite the file. A project's `statusLine`,
  permissions and unrelated hooks survive. The merge is idempotent, and it deliberately warns about
  pre-existing **non-game_loop** hooks on the events it manages, because a stray `Stop` hook from an
  older harness runs *alongside* this one and the two fight over turn-ends.
  **Do not copy that file yourself.** A wholesale copy discards the project's own settings and
  silently drops that warning.
- **What a change owes, and whether the evidence is newer than the change.** That is `verify.yaml`
  plus the commit gate, and it is resolved **per tree** — the tree a commit lands in, not the tree
  the hook script lives in. A caller does not need to reason about which record applies.
- **What is runtime state inside `.game_loop/`.** The authoritative list is `.game_loop/.gitignore`,
  written by `install.sh`. Read it rather than maintaining your own copy; it is the declaration.
- **Session compartmentalisation.** Per-session state, sibling-session awareness, and the GC that
  prunes stale sessions while never touching one holding a live mandate.

## What game_loop does not own

Anything about work that spans agents: a dependency graph, cross-process locks over shared
single-consumer resources, which agent runs what, merge order, or when to integrate. game_loop
guards one session at a time and has no opinion about the others beyond noticing they exist.

The boundary is finer than "you own the trunk" in one place worth stating: a caller decides **merge
order and timing**; game_loop decides **whether the resulting tree is verified**. Those are different
questions and the second one is already answered per-tree.

## Two tests worth stealing

**When a game_loop check fires wrongly under orchestration, is it a bug?**
Ask: *is there something recomputable that would make the warning wrong?*

- The commit blast-radius warning names files the session never wrote. Under orchestration those
  arrive by merge, and git can **prove** where they came from — a ref is recomputable. So the check
  is missing an input, and the fix is a verb that lets it see one.
- The unproved-fix warning at handback fires on an integrating run too. Nothing is recomputable
  there: a proof performed against one branch genuinely says nothing about the merged result being
  landed. Branch-green is not trunk-green. That warning is a **true positive** and the right response
  is to prove the fix again on the merge.

Same symptom, opposite diagnoses. Fan-out does not only break assumptions; sometimes it makes an
existing gap visible for the first time, and then the fix belongs upstream in the caller.

**When a fix lands here, what does it hand back up?**
Correct changes create work for the layer above. Scoping the commit gate per tree was right, and it
immediately created a question about what a freshly-created tree's harness should contain. Look for
that on purpose rather than discovering it at spawn time.

## Cross-boundary declarations must cite something recomputable

Any verb that lets a caller tell game_loop something it cannot otherwise see must take an argument
game_loop can **re-derive**, not one the caller could have invented. A ref, a real path. Never a list
of filenames — that is exactly the plausible string a model produces for free, and nothing can check
it. The recomputation *is* the check. This is INV2 ("cite the file you read") applied to assertions
that are not about files.

## Stability

The verbs, their flags, and `.game_loop/.gitignore` are the interface. Internal function names,
module layout, and the on-disk shape of `state.json` are not — do not parse them. If you need
something that is only available by reading internals, that is the missing verb again.
