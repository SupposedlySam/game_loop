---
name: gl-refused
description: Decode a game_loop refusal and take the right next step instead of working around it — the write guard ("BLOCKED: write outside this repo"), the deploy denylist, the MCP gate, the commit/verify gate, the claim gate, and the Stop gate under a mandate. Use whenever a tool call or commit is refused in a project carrying .game_loop/, or the user says "why was that blocked", "game_loop won't let me", "it refused my commit", "gl-refused", or a hook prints BLOCKED / REFUSED. Each refusal already names its own remedy; this ranks them and says which escape hatches are the human's to open, not yours.
---

# gl-refused

**A game_loop refusal is the tool working, not a bug to route around.** Every one of them prints the
remedy in its own body — the failure mode is not that the message is unclear, it is reaching for a
path that technically slips past instead of the one the message named.

Read the refusal text first. Then find its shape below.

## The one rule that outranks the rest

The gate said no because of what the call **would do**, not how it was spelled. So a rephrasing that
gets the same effect past it is not a fix — it is the same action with the guard removed. `status`
publishes exactly where the rails are blind (an interpreter one-liner, a path built from a shell
variable, an MCP mutation). That list is there so those routes are recognized and **not taken**, not
as a menu.

If the action is genuinely right, the escape hatch is always the same shape: **the human opens it.**

## `BLOCKED: write outside this repo → <path>`

The write guard. Everything outside the project is read-only — that is what makes an unattended run
safe to walk away from.

1. **Does the content belong in this repo?** Then copy it in and edit the copy. This is the answer
   most of the time and the message says so.
2. **Was it a scratch file?** The temp dir and this session's agent-memory directory are already
   allowed. Write there.
3. **Does this project legitimately write somewhere else, always?** That is config, not a bypass:
   `.game_loop/config.json` → `allow_write_roots`. Note the file is **committed** — put paths under
   the home directory in as `~/...`, never as an absolute `/Users/you/...`, or every clone inherits a
   write root only you have.
4. **Did the human explicitly authorize this one path, in words?** Then, in their words:

   ```bash
   ./.game_loop/bin/game_loop authorize --path <prefix> --reason "<their words>" [--uses N]
   ```

   Single-use by default and logged. **You do not author the reason** — if you are composing the
   human's authorization for them, there wasn't one.

Do not `cd` elsewhere and retry, and do not move the write into an interpreter. Same act, no guard.

## `BLOCKED: deploy/publish verb '<verb>'`

Irreversible and outward-facing. An unattended agent does not fire these — escalating to the human is
the only hatch, by design.

**First, check whether you were merely writing ABOUT the verb.** A commit message, an issue body or a
doc that quotes `npm publish` trips this, and the refusal names the fix: pass the prose as a file
rather than as an argument.

```bash
git commit -F <file>   ·   gh issue comment --body-file <file>   ·   <verb> --<option>-file <file>
```

The whole-word match in prose is deliberate — narrowing it would miss a real deploy nested inside an
interpreter argument, and missing a real publish is the expensive direction. If it *is* a real
deploy, stop and ask. Under a mandate, `arm` the question (see the gl-mandate skill).

## `BLOCKED: MCP tool call classified as MUTATING` / `could not be classified`

`mcp_writes` is `gated`: a mutating MCP call is refused, and the human may open it once. Unclassified
is refused for the same reason a `2` is not a `0` — unknown is not safe. Options, in order: use a
read-only call instead; ask the human to authorize; or, if this project has a standing, reviewed
grant for that exact tool, it belongs in config (`mcp_standing_writes`) as a decision on the record —
not invented mid-run.

## A refused `git commit`

Two different gates wear this coat. Read which:

- **`this commit lands in a tree that carries no game_loop`** / **`target tree is built from a
  variable`** — the gate cannot read what the change owes, so it refuses rather than passing it
  blind. Commit from the tree itself, with a literal path.
- **The verify output is the refusal body** — a change touched a path `verify.yaml` maps to a
  command, and that command has not run since the change. **Run it.** That is the entire remedy:

  ```bash
  ./.game_loop/bin/verify            # run what this change owes
  ./.game_loop/bin/verify --coverage # what is checked, what is UNCHECKED, what is excluded
  ```

  Never edit `verify.yaml` to drop the rule so the commit lands. Removing the check that just fired
  is the one move that converts a caught problem into a silent one. If the rule is genuinely wrong,
  say so to the human and change it as its own deliberate commit.

## A refused `claim`

The one hard rule: name a real file before asserting anything about external reality.

- **needs `--read`** — a path that exists, not a rephrasing of the assertion. Prose is what an LLM
  produces fluently and forgets completely; "name a file that exists" is the one check prose cannot
  satisfy. If no such file was read, the honest move is to read one, or to lower the claim to
  something the evidence supports.
- **a `--scope` claim needs two `--probe`s, on different members** — "only X" and "X is restricted"
  are claims about a *set*. One member confirms nothing about a set; the second is the price.
- **`--effector` refused, not proved this session** — a finding leaning on a verb having acted (a
  click, a scroll, a keystroke) needs that verb proved to actually act: `effector --prove`, with a
  before and an observed capture. An exit code is not a proof and is refused by name.
- **reporting a fix** — a verified diagnosis is not a verified fix. `fix --prove` wants the fix's own
  **output**, and hands the repro back to you if you offer it as proof.

## The Stop gate: a turn-end refused under a mandate

A mandate is bound, so the turn cannot end by asking the human a question, nor by announcing work
("continuing now", "I'll start on…") and then handing control back. The second is worse than the
first: it is a false statement about your own state.

Three honest endings, all in the gl-mandate skill:

```bash
game_loop checkpoint --notes ".."                          # progress, hands back, asks nothing
game_loop arm --question ".." --read <path> --predict ".."  # something you genuinely cannot derive
game_loop mandate --clear --notes ".."                      # the work is actually done
```

`--predict` is the test: if you can predict the answer, you did not need to ask.

## `guard-writes REFUSED — GAME_LOOP_HOME does not name a game_loop home`

Not your call being judged — the guard itself could not tell which project it is protecting, and
refuses rather than allowing everything. It means the wiring is wrong: usually a pinned or central
install whose hook is not passing `GAME_LOOP_HOME`. Re-run the installer for this repo (the
gl-install skill), then start a new session, since hooks are read at session start.

## Never

- Never disable, edit, or route around a guard to make your own current call succeed. If a guard is
  genuinely wrong, that is a separate change, made deliberately, and it does not ship inside the work
  that tripped it.
- Never write the human's `--reason` or `--because` for them.
- Never treat `⚠ HOOKS NOT LIVE` as "the gates are off, proceed freely" — it means they are
  registered and firing on nothing, which is the least safe state, not the most convenient one.
