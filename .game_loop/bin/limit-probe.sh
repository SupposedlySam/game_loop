#!/usr/bin/env bash
# Ask the HOST what your usage windows look like, when this host cannot tell you itself.
#
# WHY THIS EXISTS. `rate_limits` rides the statusline payload, and a statusline is a TUI element: an
# editor-embedded session renders none, so limitgate, the watchdog's park and the wake-on-reset are
# all inert there. This spends ONE short headless session to read the windows instead.
#
# HOW, SINCE 2026-11-16: a headless `claude -p --output-format stream-json --verbose` run prints a
# `rate_limit_event` whose `rate_limit_info.unifiedWindows` carries each window's SERVER-SIDE
# utilization (a 0-1 fraction) and resetsAt (epoch seconds). Reported in game_loop#146 by the drops
# owner agent (2.1.319) and measured here on 2.1.322 before switching. The probe used to drive the
# full TUI through a pty with a statusline tap, because a headless run never invokes the statusline;
# this event is why it no longer has to.
#
# WHAT IT COSTS, MEASURED 2026-11-16 on 2.1.322 with hooks disabled and no MCP servers: 2 input,
# 14,396 cache-creation and 11,382 cache-read, ~25.8k input-side, in a few seconds. The pty render
# measured ~64k (2026-08-24), and ran for most of a minute. The number is this machine's preamble at
# that moment, not a constant; it is still one spawned session per reading, which is why the probe
# is off unless a project enables it.
#
# CONTRACT (unchanged): prints {"rate_limits": {...}, "context_window": null} on stdout and exits 0,
# with each window as {"used_percentage": <0-100>, "resets_at": <epoch seconds>}, the statusline's
# own shape, so every consumer reads it unchanged. Exit 1 means it ran and the host sent no
# rate_limit_event (a real answer: an unsubscribed or API-key account has none). Exit 2 means it
# could not tell: no binary, the run failed or timed out. "Could not look" and "looked and there is
# nothing" must never be the same bytes.
#
# context_window is null: a headless run renders no statusline payload to read it from. The consumer
# records that as an explicit null, which is what it already did when a render lacked one.
set -uo pipefail

# The running binary, not whatever is on PATH: the running one is the account this probe asks about.
# GAME_LOOP_PROBE_BIN is for the suite only, to drive this parser against a recorded stream.
CB="${GAME_LOOP_PROBE_BIN:-${CLAUDE_CODE_EXECPATH:-}}"
[ -n "$CB" ] && [ -x "$CB" ] || CB="$(command -v claude 2>/dev/null || true)"
if [ -z "$CB" ]; then
  echo "no claude binary (CLAUDE_CODE_EXECPATH unset and none on PATH)" >&2; exit 2
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/gl-limitprobe-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

CB="$CB" WORK="$WORK" TIMEOUT="${GAME_LOOP_PROBE_WAIT_SEC:-90}" python3 <<'PY'
import json, os, subprocess, sys

# --strict-mcp-config with no MCP config and every hook disabled: the cheapest session the host
# allows, and one that cannot register with the chat waker or this harness's own hooks. Run from the
# probe's scratch directory so no project settings are read.
cmd = [os.environ["CB"], "-p", "reply with the single word: ok", "--output-format", "stream-json",
       "--verbose", "--strict-mcp-config", "--settings", '{"disableAllHooks":true}']
try:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=int(os.environ["TIMEOUT"]),
                       cwd=os.environ["WORK"])
except subprocess.TimeoutExpired:
    print("the headless run did not finish within %ss" % os.environ["TIMEOUT"], file=sys.stderr)
    sys.exit(2)
except OSError as e:
    print("could not run %s: %s" % (os.environ["CB"], e), file=sys.stderr)
    sys.exit(2)

windows = None
for line in (r.stdout or "").splitlines():
    try:
        d = json.loads(line)
    except ValueError:
        continue                                   # a partial line is not a verdict
    if not isinstance(d, dict) or d.get("type") != "rate_limit_event":
        continue
    uw = ((d.get("rate_limit_info") or {}).get("unifiedWindows")) or {}
    got = {}
    for name, w in uw.items():
        try:
            got[name] = {"used_percentage": round(float(w["utilization"]) * 100, 2),
                         "resets_at": float(w["resetsAt"])}
        except (KeyError, TypeError, ValueError):
            continue
    if got:
        windows = got                              # keep the LAST, which is the freshest

if windows is None:
    if r.returncode != 0:
        print("the headless run failed (exit %d) before any rate_limit_event: %s"
              % (r.returncode, (r.stderr or "").strip()[-200:]), file=sys.stderr)
        sys.exit(2)
    print("ran, but the host sent no rate_limit_event (an unsubscribed or API-key account has none)",
          file=sys.stderr)
    sys.exit(1)
print(json.dumps({"rate_limits": windows, "context_window": None}))
PY
