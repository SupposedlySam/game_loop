# LEDGER

Reference knowledge for this project. **Not a gate.** It informs hypotheses; it never blocks them.

The failure mode this file has a name for: *ledger rot* — a confidently-wrong RULED-OUT permanently
fencing off the right answer. So every entry carries its **source path** and its **date**, and
RULED-OUT means *we read the thing and it said no*, never *we tried once and it didn't work*.

Three buckets. Append as you learn.

---

## VERIFIED

_Things we read at the pinned version, by hand, and confirmed. Each with a source path + date._

- **Claude Code exposes subscription rate limits in the statusline stdin JSON**:
  `rate_limits.five_hour` / `.seven_day`, each `{used_percentage: 0–100, resets_at: unix epoch}`.
  Subscriber-only (Pro/Max), appears after the first API response, each window independently absent.
  Source: https://code.claude.com/docs/en/statusline.md ("Available data" table + example payload),
  read 2026-07-29. This is the tap `game_loop statusline` builds on.
- **statusLine config keys**: `{type: "command", command, refreshInterval (min 1s), padding}`;
  event-driven runs debounced at 300ms, `refreshInterval` adds a timer for idle periods. Same source,
  2026-07-29.

## RULED-OUT

_Things we read the source for and confirmed are NOT the case. Not "tried once and it didn't work."_

- **Hook payloads carry rate-limit data** — they don't (no field on any event; an `OnRateLimit` hook
  is an open feature request: anthropics/claude-code#34817). Checked 2026-07-29.
- **A headless `claude usage` / `--usage` flag exists** — it doesn't; open requests
  anthropics/claude-code#44328, #40395, #39141. A rate-limited `claude -p` exits 1 with an
  undocumented message and no reset timestamp. Checked 2026-07-29.
- **`anthropic-ratelimit-unified-*` response headers are readable by harness code** — they exist
  API-side but Claude Code does not persist them anywhere scripts can reach (open request:
  anthropics/claude-code#55333). The statusline fields are the client-side derivative of these
  headers and the only exposed form. Checked 2026-07-29.
- **The per-model weekly (Opus) limit is in the statusline payload** — it isn't; only `five_hour`
  and `seven_day` windows exist there (statusline.md, 2026-07-29). The limit machinery therefore
  cannot see it; a run can still die on it unwarned.

## OPEN

_Questions still outstanding. What would close each one._

- **Does a Stop hook fire on the turn a rate-limit error kills?** Unconfirmed either way; the park
  design deliberately doesn't depend on it (the watchdog armed by the *previous* turn-end reads
  limits.json before spending its ring budget). Closed by: observing a real limit death with the
  probe payload present.
- **Live-fire the Slack + park path end-to-end** (real workspace, real 5h exhaustion). The fake-server
  suite proves the logic; a real run proves the wiring. Closed by: one observed park → page → reset →
  resume cycle in log.jsonl.
- **Do limit-deaths cluster at a consumption level that ordinary session closures do not?** This is
  the whole of whether #45's usage estimator can ever be built rather than invented. Every turn-end
  already records a `usage_window` reading and nothing compares it to a threshold, on purpose.
  **AND WE NOW KNOW WHY IT IS LIKELY TO STAY OPEN**, which is worth more than the question: the
  population that would settle it does not exist naturally. Asked of a consumer running 18
  concurrent sessions — the right size and the right host — the answer was 43 readings across four
  sessions with **zero labelled limit-deaths**, and that gap is structural rather than a lack of
  digging. Limit protection is inert on editor hosts (no statusline, so no snapshot), and a session
  cannot observe its own death, so every session there reads as an ordinary closure whether it was
  one or not. Producing a positive class would mean staging sessions for the experiment rather than
  instrumenting work someone was doing anyway, and that consumer declined on exactly that ground —
  correctly, since a staged run is a different measurement wearing this one's name.
  Unlabelled, their consumption clusters at 300–440k output tokens per 5h window, which is a
  precondition for a gauge and not evidence for one.
  Closed by: several long unattended runs from a *terminal* host, each labelled died-at-limit or
  closed-deliberately, with their `usage_window` records. Source: issue #60, 2026-08-05.
  **Do not re-derive this by asking another consumer with the same host shape** — the answer will be
  the same one-class sample, and "for want of looking" is a known state rather than a guessed number.
