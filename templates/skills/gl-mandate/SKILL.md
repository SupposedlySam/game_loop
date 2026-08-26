---
name: gl-mandate
description: Run a game_loop session unattended, end to end — bind a mandate so the Stop gate goes live, checkpoint progress without asking anything, arm the one question that genuinely cannot be derived, park and resume when a human calls a break, and clear it when the work is actually done. Also covers what makes walking away safe: the watchdog, Slack paging, and the usage-limit park. Use when the user says "run unattended", "work on this while I'm out", "set a mandate", "gl-mandate", "resume the mandate", "what is this session supposed to be doing", or a turn-end is refused because a mandate is bound.
---

# gl-mandate

A mandate is the difference between a session that stops when you look away and one that keeps
working. Binding one makes the **Stop gate live**: turn-ends that ask the human a question, or that
announce work and then hand control back, stop being possible.

Everything below is `./.game_loop/bin/game_loop <verb>` — project-local, never a global command.

## Before binding: is one already bound?

```bash
./.game_loop/bin/game_loop status
```

Read the `MANDATE:` line and do not bind over it:

| Line | State | Do |
|---|---|---|
| `none (Stop gate inert)` | ordinary session | bind, if the human is leaving |
| bound text | work is open, gate is live | pick that work up — do not re-bind |
| `⏸ PARKED` | a human interrupted open work | `mandate --resume` **first**, then continue |

Mandate state is **per session** (`.game_loop/sessions/<id>/`), so another session's mandate gates
that session and not this one.

## Bind

```bash
./.game_loop/bin/game_loop mandate --set "<the work, in the human's words>"
```

In **their** words. The mandate is what the Stop gate holds the session to and what a later session
rehydrates from after compaction; a paraphrase that drifts toward what is easy to finish is how a
run ends early and reports success.

## The three honest ways a turn can end while it is bound

**1 — Progress, handing back, asking nothing.** The default.

```bash
./.game_loop/bin/game_loop checkpoint --notes "<what you did and what happens next>"
```

**2 — A question you genuinely cannot answer yourself.** One interruption of the human is the most
expensive rung on the cost ladder, so it is rationed:

```bash
./.game_loop/bin/game_loop arm --question "<what you need>" \
                              --read <path you ALREADY read that did not answer it> \
                              --predict "<what you expect them to say>"
```

`--predict` is the test, and it is aimed at you: **if you can predict the answer, you did not need to
ask.** `--read` is the same rule as the claim gate — exhaust the cheap rungs (read the source, run
the thing) before spending the human's attention.

**3 — Done.** Not "the easy part is done":

```bash
./.game_loop/bin/game_loop mandate --clear --notes "<why it is genuinely satisfied>"
```

## The wake path — `status` asks for it and this is how you answer

Binding a mandate arms every gate that lives **inside** the session: the Stop gate, the watchdog, the
limit gate. All of them fire from in here, which is the thing that stops working when a run goes
quiet. So `status` warns:

```
⚠ MANDATE ARMED, AND NO EXTERNAL WAKE PATH IS RECORDED.
```

Answer it on the mandate you already bound — no `--set`, so the human's words are not retyped and
cannot be paraphrased in passing:

```bash
./.game_loop/bin/game_loop mandate --wake-path "<how a signal reaches this session while it is idle>"
```

A cron that pokes the session, a Stop-hook waker, a human who checks. **It is DECLARED, never
probed** — the tool cannot test that the path delivers, and a declared path that has stopped
delivering reads exactly like one that works. Recording it is worth less than a probe; not recording
it is worth nothing.

If the honest answer is "a human who checks", write that. It is the common case, and a run that says
so is more use to a successor than one that leaves the field blank.

## Park and resume — a human called a break

Parking is **not** clearing. It pauses the gate while leaving the work open, so nothing reads as
finished:

```bash
./.game_loop/bin/game_loop mandate --park --reason "<their words, verbatim>" --next "<step to pick back up>"
./.game_loop/bin/game_loop mandate --resume        # they are back; the gate is live again
```

`--reason` is required and it is theirs, not yours. A parked mandate that nobody resumes is the state
`status` reports as `⏸ PARKED` at the top of every later session — which is the point.

## What actually makes walking away safe

The Stop gate stops the session **saying** the wrong thing at turn-end. It cannot make it **do** the
next thing — that is the watchdog, wired as a background Stop hook. It notices when game_loop's state
says work is outstanding while the transcript has stopped growing, and rings the session back to
work. Three things worth checking before a long unattended run:

- **Hooks are live.** `status` printing `⚠ HOOKS NOT LIVE`, or a Stop-gate probe far older than the
  session's own activity, means turn-ends are passing unchecked. Hooks are read at session start —
  reload the window or start a new session, then re-read the line.
- **Paging is configured**, if anyone is expected to answer an armed question:

  ```bash
  ./.game_loop/bin/game_loop notify --test
  ```

  Config lives in `.game_loop/notify.json` (gitignored — it holds a credential; schema in
  `.game_loop/bin/notify.py`). With it live, an armed T3 question pages Slack and a reply in that
  thread clears the arm and rings the answer back into the run, so the desk is optional.
- **Usage-limit survival is armed.** `⚠ USAGE-LIMIT PROTECTION IS INERT` means the statusline tap
  carried no rate-limit windows, so nothing pages at the threshold and nothing parks at exhaustion —
  a limit will stop the run in silence. On an API-key session those windows are simply not exposed
  and the gates cannot arm; that is worth saying out loud to the human before they walk away, rather
  than discovering it from a run that died quietly.
- **Hand over with the verb, not by hand.** When the context fills, `game_loop successor` is what
  starts the next session — and it is also what stands THIS session's watchdog down, by recording the
  handover where the watchdog reads it. Hand off any other way and the retired session keeps ringing
  itself back into a mandate the new one already owns: two sessions driving one goal, which is a real
  logged failure and not a hypothetical. `status` shows a retired session as `⇢ HANDED OVER`.
  One thing does NOT travel: a T3 question armed before the handover. The arm lives in the old
  session's state, so put the question in the handoff file — `successor` says so when it sees one.
  One thing does not travel by default either: **permission bypass**. An unattended successor that
  opens on a permission prompt stalls where nobody is watching, and the flag is read at launch, so
  the successor cannot grant it to itself. `limits.successor.skip_permissions: true` in
  `.game_loop/config.local.json` — the GITIGNORED file, not the tracked one — puts
  `--dangerously-skip-permissions` on the command `successor` builds. There is deliberately no flag
  for it, and both write rails refuse that file, so a session that grants it must spend `authorize`
  and leave a human's words in the log; set it in the tracked `config.json` and `successor` says so
  and ignores it. That is a refused-by-default door with an audit trail, NOT prevention — a
  `python3 -c` still writes any file here. It does NOT reach the `saggar-agent` mode, which builds
  its own invocation and says so.

## Never

- Never clear a mandate to end a turn. That is the one move that converts unfinished work into a
  clean-looking finish, and it is indistinguishable afterwards from having done the job.
- Never re-bind over a `⏸ PARKED` mandate — resume it. Re-binding writes over the open work's record.
- Never write the human's `--reason` for a park, or their words for a `--set`.
- Never treat an armed question as a turn-end substitute for work you could have done. `--read` and
  `--predict` exist to make that visible.
