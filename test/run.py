#!/usr/bin/env python3
"""game_loop's own guarantees, checked rather than remembered. Run:  python3 test/run.py

Drives the REAL scripts through their real interfaces (CLI args, stdin JSON) inside a throwaway copy
of .game_loop, so a regression in any gate fails here instead of in production. No dependencies.
"""
import ast
import contextlib
import datetime
import io
import importlib.machinery
import importlib.util
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_GAME_LOOP = os.path.join(REPO, ".game_loop")

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


def make_sandbox():
    """A temp project with a fresh .game_loop (real scripts, empty state)."""
    proj = tempfile.mkdtemp(prefix="gameloop-test-")
    dst = os.path.join(proj, ".game_loop")
    os.makedirs(os.path.join(dst, "bin"))
    for f in ("game_loop", "watchdog", "guard-writes.sh", "guard-writes-impl.sh",
              "guard-mcp.sh", "guard-mcp-impl.sh", "verify", "flair.py", "notify.py",
              "limit-probe.sh"):
        shutil.copy(os.path.join(SRC_GAME_LOOP, "bin", f), os.path.join(dst, "bin", f))
        os.chmod(os.path.join(dst, "bin", f), 0o755)
    for f in ("config.json", "verify.yaml", "INVARIANTS.md"):
        shutil.copy(os.path.join(SRC_GAME_LOOP, f), os.path.join(dst, f))
    # The agent brief ships with the payload (#60) and a refusal points at it, so a fixture without
    # it is not a faithful install — it would let a pointer-names-a-real-file assertion pass here
    # and fail for every consumer, which is the direction that matters.
    shutil.copy(os.path.join(REPO, "llms.txt"), os.path.join(dst, "llms.txt"))
    shutil.copy(os.path.join(SRC_GAME_LOOP, "claims.json"), os.path.join(dst, "claims.json"))
    return proj


def _env(proj=None, sid=None, **extra):
    """A controlled environment: the suite itself may run inside a Claude session, and its
    CLAUDE_CODE_SESSION_ID leaking into the scripts under test would silently session-scope
    every 'legacy' check. Scrub, then set exactly what the test names."""
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("GAME_LOOP_SESSION", None)
    # Same reason, one variable later (#34): the suite runs inside a Claude session that sets
    # CLAUDE_CODE_ENTRYPOINT, and letting it through means a test asking "what happens with NO
    # entrypoint" silently gets this one instead — the arm never varies and passes for the wrong
    # reason. A test whose control is contaminated by the runner cannot fail.
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    # Same reason again: this suite may itself be guarded by a PINNED harness, whose hooks export
    # GAME_LOOP_HOME. Letting it through would point every sandboxed script at the REAL repo's
    # .game_loop — the tests would write their state into it and every "unset" control would
    # silently be an "override set" case, passing for the wrong reason.
    env.pop("GAME_LOOP_HOME", None)
    # And once more for the terminal (#78): `successor` resolves mode "auto" by reading TERM_PROGRAM,
    # and this suite is typically run FROM Warp. Letting it through would mean every test of the
    # portable floor silently exercised the warp-tab path instead — writing ~/.warp/tab_configs and
    # opening real tabs on the developer's screen, from a test asserting nothing was opened.
    env.pop("TERM_PROGRAM", None)
    if proj:
        env["CLAUDE_PROJECT_DIR"] = proj
    if sid:
        env["GAME_LOOP_SESSION"] = sid
    env.update(extra)
    return env


def gl(proj, *args, stdin=None, sid=None, **envx):
    return subprocess.run([os.path.join(proj, ".game_loop", "bin", "game_loop"), *args],
                          input=stdin, capture_output=True, text=True, env=_env(sid=sid, **envx))


# The watchdog can legitimately BLOCK FOREVER: poll_slack_replies is a `while True:` whose exits all
# wait on a reply arriving, on the arm going away, or on a newer watchdog superseding it. That is
# right in production — a human who has not answered still holds the ball — and wrong for a test,
# which has no bound of its own. Neutering notify.replies made the suite HANG rather than fail: the
# 300s cap was hit, the whole baseline reported as killed because no assertion finished, and from
# outside it was indistinguishable from a slow machine (#50).
#
# So the TEST is bounded and the product loop is left alone. A timeout comes back as a result with a
# named failure rather than as silence.
WATCHDOG_TIMEOUT_SEC = 30


class _TimedOut:
    """A bounded run that did not finish. Shaped like a CompletedProcess so callers read the same,
    and deliberately NOT returncode 2 — a hang must never satisfy an assertion about a ring."""
    returncode = None

    def __init__(self, secs):
        self.stdout = ""
        self.stderr = f"TIMED OUT after {secs}s — the watchdog never returned"


def run_watchdog(wd_bin, payload, **env):
    try:
        return subprocess.run([wd_bin], input=json.dumps(payload), capture_output=True, text=True,
                              timeout=WATCHDOG_TIMEOUT_SEC, env=_env(**env))
    except subprocess.TimeoutExpired:
        return _TimedOut(WATCHDOG_TIMEOUT_SEC)


def guard(proj, payload, sid=None):
    return subprocess.run([os.path.join(proj, ".game_loop", "bin", "guard-writes.sh")],
                          input=json.dumps(payload), capture_output=True, text=True,
                          env=_env(proj, sid))


def denied(res):
    """The guard always exit 0; a deny is a JSON body with permissionDecision=deny."""
    return '"permissionDecision":"deny"' in res.stdout or '"permissionDecision": "deny"' in res.stdout


def _probe_f(proj, payload, sid=None):
    """Where the write guard's invocation mark lives — beside the state it is scoped to, exactly
    like edited.txt. Mirrors the SID resolution in guard-writes-impl.sh: the payload's session_id
    wins, then the environment's, then the repo-global fallback."""
    raw = (payload.get("session_id") or sid or "")
    s = re.sub(r"[^A-Za-z0-9._-]", "-", raw.strip())[:64]
    base = os.path.join(proj, ".game_loop")
    return os.path.join(base, "sessions", s, "write-guard-probe") if s \
        else os.path.join(base, "write-guard-probe")


def _probe_count(f):
    try:
        with open(f) as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def allowed(proj, payload, sid=None):
    """THE PERMISSIVE ASSERTION, with the second bit it needs (#41).

    `not denied(...)` is not enough here, and that was measured rather than argued: replacing
    guard-writes-impl.sh with a script that parses and exits 0 — a guard present, wired, live and
    checking nothing — left sixteen "allows…" assertions in this file green. A refusal cannot be
    produced by absence, so every BLOCK assertion validates itself; but this guard's ALLOW is
    SILENCE, and silence is exactly what a dead guard emits. `allowed` and `never ran` were the same
    observation.

    The fix a downstream project used for the same defect — assert the guard's REASON instead of its
    verdict — needs a guard that SPEAKS when it permits. This one does not, so the guard carries a
    MARK it advances before its first early return, and a permissive assertion requires the mark to
    have MOVED as well as the tool to have been allowed. Neither a no-op impl nor a shim that failed
    open can produce that.

    So: allowed == the guard ran AND it did not deny.
    """
    f = _probe_f(proj, payload, sid)
    before = _probe_count(f)
    res = guard(proj, payload, sid)
    return _probe_count(f) > before and not denied(res)


def main():
    proj = make_sandbox()
    try:
        print("claim gate:")
        check("refuses a nonexistent --read path",
              gl(proj, "claim", "--assert", "x", "--read", "/no/such/file").returncode != 0)
        real = os.path.join(proj, ".game_loop", "config.json")
        check("accepts a real --read path",
              gl(proj, "claim", "--assert", "x", "--read", real).returncode == 0)

        print("stop gate (no mandate = inert):")
        r = gl(proj, "stopgate", stdin='{"last_assistant_message":"want me to continue?"}')
        check("inert with no mandate → allows", r.returncode == 0)

        print("stop gate (mandate bound):")
        gl(proj, "mandate", "--set", "do the work")
        r = gl(proj, "stopgate", stdin='{"last_assistant_message":"Done part 1. Want me to do part 2?"}')
        check("blocks a question (exit 2)", r.returncode == 2)
        r = gl(proj, "stopgate", stdin='{"last_assistant_message":"Continuing to the next step now."}')
        check("blocks announce-then-stop (exit 2)", r.returncode == 2)
        gl(proj, "checkpoint", "--notes", "reporting")
        r = gl(proj, "stopgate", stdin='{"last_assistant_message":"Finished the batch. Remaining: docs."}')
        check("allows a checkpointed report (exit 0)", r.returncode == 0)
        # checkpoint is consumed — a second bare stop blocks again
        r = gl(proj, "stopgate", stdin='{"last_assistant_message":"Still going on the batch."}')
        check("checkpoint is single-use (next bare stop blocks)", r.returncode == 2)

        print("stop gate (arm → consume):")
        gl(proj, "arm", "--question", "which color?", "--read", real, "--predict", "blue")
        r = gl(proj, "stopgate", stdin='{"last_assistant_message":"Which color do you want?"}')
        check("armed question passes once (exit 0)", r.returncode == 0)
        r = gl(proj, "stopgate", stdin='{"last_assistant_message":"And which size?"}')
        check("arm is consumed (next question blocks)", r.returncode == 2)
        gl(proj, "mandate", "--clear", "--notes", "done")

        print("write guard:")
        inside = os.path.join(proj, "file.txt")
        # `allowed`, not `not denied`: for a guard whose allow is silence, the verdict alone is also
        # what a guard that never ran produces, so each of these requires the guard's mark to have
        # advanced too (#41 — see allowed()).
        check("allows a write inside the repo",
              allowed(proj, {"tool_name": "Write", "tool_input": {"file_path": inside}}))
        check("denies a write to another dir",
              denied(guard(proj, {"tool_name": "Write",
                                  "tool_input": {"file_path": os.path.expanduser("~/evil.txt")}})))
        check("denies rm behind a cd into another tree",
              denied(guard(proj, {"tool_name": "Bash",
                                  "tool_input": {"command": "cd ~ && rm -rf somedir"}})))
        check("allows normal in-repo bash",
              allowed(proj, {"tool_name": "Bash",
                             "tool_input": {"command": "rm -f file.txt && echo hi > b.txt"}}))
        check("allows cp OUT of another tree into the repo",
              allowed(proj, {"tool_name": "Bash",
                             "tool_input": {"command": "cp ~/.bashrc ./copy"}}))
        check("allows redirecting to /dev/null (a discard device)",
              allowed(proj, {"tool_name": "Bash",
                             "tool_input": {"command": "grep x file.txt 2>/dev/null"}}))
        check("allows redirecting to a std stream (/dev/stderr)",
              allowed(proj, {"tool_name": "Bash",
                             "tool_input": {"command": "echo hi >/dev/stderr"}}))
        # #7: a DATA heredoc body (fed to cat/tee) is not executed shell — redirect-like prose in it
        # must not be flagged. But a CODE heredoc body (fed to bash/sh/...) DOES run and must stay
        # guarded, or the fix would open a bypass. Both directions are asserted.
        check("allows out-of-repo redirect text inside a cat (data) heredoc body",
              allowed(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "cat <<'EOF'\nnote: echo x > ~/outside.txt\nEOF"}}))
        check("still denies rm of an out-of-repo path inside a bash (code) heredoc body",
              denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "bash <<'EOF'\nrm -rf ~/outside\nEOF"}})))
        check("still denies an out-of-repo redirect inside a bash (code) heredoc body",
              denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "bash <<'EOF'\necho x > ~/outside.txt\nEOF"}})))
        # #8: a malformed guard must FAIL OPEN, never exit-2 block — otherwise a broken guard blocks
        # its own repair. The shim `bash -n`s the impl and allows the tool when the impl won't parse.
        impl_f = os.path.join(proj, ".game_loop", "bin", "guard-writes-impl.sh")
        with open(impl_f) as f:
            impl_src = f.read()
        with open(impl_f, "w") as f:
            f.write("this is ( not valid bash\n")
        broken = guard(proj, {"tool_name": "Bash", "tool_input": {"command": "rm -rf ~/outside"}})
        check("fails OPEN when the guard impl is malformed (can't block its own fix)",
              not denied(broken))
        # #39: failing open is right; failing open in SILENCE is not. With no output at all, a
        # guard that is ABSENT is indistinguishable from one that ran and was content — INV3 stops
        # being enforced and nothing says so, and a syntax error is exactly the edit an agent makes
        # while working ON this guard. Note the check ABOVE passes either way: it asserts only the
        # allow, which is precisely how the silence survived. So assert the NOTICE.
        check("...and SAYS SO — a silent fail-open is INV3 switched off with no signal (#39)",
              "WRITE GUARD IS NOT RUNNING" in broken.stdout and "INV3" in broken.stdout)
        with open(impl_f, "w") as f:                 # restore so later checks use the real guard
            f.write(impl_src)

        # #41: the check above catches a guard that will not PARSE. It cannot catch a guard that
        # parses fine and checks nothing — the shim execs that one, so #39's notice never fires and
        # the tool is allowed in silence, exactly as a working guard allows it. Measured, not
        # supposed: neutering the impl to `exit 0` left sixteen "allows…" assertions here green.
        # A refusal validates itself, because absence cannot produce one; a silent allow does not.
        # So the guard carries a MARK it advances before its first early return, and these assert the
        # mark's contract — the thing every permissive assertion above now leans on.
        print("write guard (a silent allow must still carry evidence the guard ran — #41):")
        probe_f = _probe_f(proj, {})
        n0 = _probe_count(probe_f)
        guard(proj, {"tool_name": "Write", "tool_input": {}})
        check("the mark advances on a Write with no file_path — the guard's EARLIEST early return",
              _probe_count(probe_f) > n0)
        n0 = _probe_count(probe_f)
        guard(proj, {"tool_name": "Read", "tool_input": {"file_path": inside}})
        check("...and on a tool the guard's case statement never names",
              _probe_count(probe_f) > n0)
        n0 = _probe_count(probe_f)
        d = guard(proj, {"tool_name": "Write",
                         "tool_input": {"file_path": os.path.expanduser("~/evil2.txt")}})
        check("...and on a DENY: the mark says the guard RAN, never what it decided",
              denied(d) and _probe_count(probe_f) > n0)
        sprobe = _probe_f(proj, {}, sid="sess-probe")
        guard(proj, {"tool_name": "Write", "tool_input": {"file_path": inside}}, sid="sess-probe")
        check("the mark is per-session, beside that session's state — scoped like edited.txt",
              _probe_count(sprobe) > 0
              and os.path.dirname(sprobe) == os.path.dirname(
                  os.path.join(proj, ".game_loop", "sessions", "sess-probe", "state.json")))

        # THE DEFECT ITSELF, encoded so it cannot come back: a guard that is present, wired, live and
        # checking nothing. It PARSES, so the shim execs it and the fail-open notice stays silent —
        # and the tool is allowed. `not denied(...)` passes here; `allowed(...)` must not.
        with open(impl_f, "w") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(impl_f, 0o755)
        dead = guard(proj, {"tool_name": "Bash", "tool_input": {"command": "rm -rf ~/outside"}})
        check("a guard that PARSES and checks nothing still allows, and says nothing (the defect)",
              not denied(dead) and "WRITE GUARD IS NOT RUNNING" not in dead.stdout)
        check("...but it CANNOT advance the mark, so a permissive assertion now fails on it",
              not allowed(proj, {"tool_name": "Write", "tool_input": {"file_path": inside}}))
        with open(impl_f, "w") as f:                 # restore so later checks use the real guard
            f.write(impl_src)
        os.chmod(impl_f, 0o755)
        check("...and the real guard passes that same assertion (the probe is not always-false)",
              allowed(proj, {"tool_name": "Write", "tool_input": {"file_path": inside}}))

        # INV5: a probe that can break the thing it observes is worse than no probe. Make the mark
        # unwritable in the only way no permission bit can undo — the path is a DIRECTORY — and the
        # guard must go on guarding.
        os.makedirs(_probe_f(proj, {}, sid="sess-noprobe"), exist_ok=True)
        blocked = guard(proj, {"tool_name": "Write",
                               "tool_input": {"file_path": os.path.expanduser("~/evil3.txt")}},
                        sid="sess-noprobe")
        check("a mark that cannot be written costs the MARK, never the guarding (INV5)",
              blocked.returncode == 0 and denied(blocked))

        print("write guard (authorize → consume):")
        gl(proj, "authorize", "--path", os.path.expanduser("~/authztest"),
               "--reason", "user said ok")
        p = {"tool_name": "Bash", "tool_input": {"command": "touch ~/authztest/x"}}
        check("authorized path allowed once", allowed(proj, p))
        check("authorization is single-use (spent → denied)", denied(guard(proj, p)))
        # #1: the escape hatch must work for the Write/Edit tools too, not just Bash mutators —
        # the deny message points at `authorize`, so `authorize` has to unblock this path.
        gl(proj, "authorize", "--path", os.path.expanduser("~/authztest-write"),
               "--reason", "user said ok")
        pw = {"tool_name": "Write",
              "tool_input": {"file_path": os.path.expanduser("~/authztest-write/x.md")}}
        check("authorized path allowed once via Write", allowed(proj, pw))
        check("Write authorization is single-use (spent → denied)", denied(guard(proj, pw)))
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            log = f.read()
        check("a Write spend is logged as authorized_write",
              '"authorized_write"' in log and "authztest-write" in log)

        print("deploy verbs:")
        cf = os.path.join(proj, ".game_loop", "config.json")
        c = json.load(open(cf)); c["deploy_verbs"] = ["firebase deploy"]
        json.dump(c, open(cf, "w"))
        check("blocks a configured deploy verb",
              denied(guard(proj, {"tool_name": "Bash",
                                  "tool_input": {"command": "firebase deploy --only hosting"}})))

        # #2: state is compartmentalized per Claude Code session. A mandate one session binds must
        # never gate another session (or a no-session invocation) sharing the same checkout — that
        # cross-talk sent an unrelated session off to "resume" work it was never asked to do.
        print("per-session state (isolation):")
        gl(proj, "mandate", "--set", "session A work", sid="sess-aaa")
        r = gl(proj, "stopgate",
               stdin='{"session_id":"sess-aaa","last_assistant_message":"Should I continue?"}')
        check("A's stopgate enforces A's mandate", r.returncode == 2)
        r = gl(proj, "stopgate",
               stdin='{"session_id":"sess-bbb","last_assistant_message":"Should I continue?"}')
        check("A's mandate does not gate session B", r.returncode == 0)
        r = gl(proj, "stopgate", stdin='{"last_assistant_message":"Should I continue?"}')
        check("A's mandate does not gate a no-session stop", r.returncode == 0)
        gl(proj, "checkpoint", "--notes", "B reporting", sid="sess-bbb")
        r = gl(proj, "stopgate",
               stdin='{"session_id":"sess-aaa","last_assistant_message":"Status update."}')
        check("B's checkpoint does not license A's turn-end", r.returncode == 2)
        gl(proj, "checkpoint", "--notes", "A reporting", sid="sess-aaa")
        r = gl(proj, "stopgate",
               stdin='{"session_id":"sess-aaa","last_assistant_message":"Status update."}')
        check("A's own checkpoint licenses A", r.returncode == 0)
        # authorizations are granted IN a session and spendable only there
        gl(proj, "authorize", "--path", os.path.expanduser("~/sessauth"),
           "--reason", "user said ok", sid="sess-aaa")
        pb = {"tool_name": "Bash", "tool_input": {"command": "touch ~/sessauth/x"},
              "session_id": "sess-bbb"}
        check("A's authorization is not spendable in session B", denied(guard(proj, pb)))
        pb["session_id"] = "sess-aaa"
        check("A's authorization spends in session A", not denied(guard(proj, pb)))

        print("watchdog (per-session):")
        tpath = os.path.join(proj, "transcript.jsonl")
        with open(tpath, "w") as f:
            f.write("x\n")
        wd_bin = os.path.join(proj, ".game_loop", "bin", "watchdog")

        def watchdog(sid_payload):
            payload = {"transcript_path": tpath}
            if sid_payload:
                payload["session_id"] = sid_payload
            return run_watchdog(wd_bin, payload,
                                WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0")
        check("watchdog rings for its own session's idle mandate (A)",
              watchdog("sess-aaa").returncode == 2)
        check("watchdog stays quiet for a session with no mandate (C)",
              watchdog("sess-ccc").returncode == 0)
        check("watchdog pidfile is per-session, not repo-global",
              os.path.exists(os.path.join(proj, ".game_loop", "sessions", "sess-aaa",
                                          ".watchdog.pid"))
              and not os.path.exists(os.path.join(proj, ".game_loop", ".watchdog.pid")))
        # shared log carries per-session attribution
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            log = f.read()
        check("shared log lines carry the writing session's sid", '"sid": "sess-aaa"' in log)
        gl(proj, "mandate", "--clear", "--notes", "done", sid="sess-aaa")

        # #5: quoted DATA is not shell. Redirect chars and deploy verbs inside a message-flag string
        # or a sed script must not deny; a QUOTED target of a real redirect must still deny (the old
        # regex let it through). Interpreter args are not message flags and stay guarded.
        print("write guard (quoted text is data):")
        check("allows a redirect mentioned inside a commit -m message",
              allowed(proj, {"tool_name": "Bash", "tool_input": {
                  "command": 'git commit -m "note: echo x > ~/outside.txt"'}}))
        check("allows a deploy verb mentioned inside a commit -m message",
              allowed(proj, {"tool_name": "Bash", "tool_input": {
                  "command": 'git commit -m "docs: describe the npm publish flow"'}}))
        check("allows a redirect char inside a sed script",
              allowed(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "env | sed 's/=.*TOKEN.*/=<redacted>/'"}}))
        check("still denies a real redirect to a QUOTED out-of-repo target",
              denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": 'echo x > "$HOME/gl_outside.txt"'}})))
        check("still denies a deploy verb inside an interpreter arg (bash -c executes)",
              denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "bash -c 'npm publish'"}})))
        check("allows a data heredoc whose opener also has a redirect (consumer is cat, not the target)",
              allowed(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "cat > out.md <<'EOF'\nnote: echo x > ~/outside.txt\nEOF"}}))

        # #4: the commit gate applies only to commits that TARGET this repo — verify.yaml describes
        # THIS repo's owed checks; a commit in some other repository owes that repo's checks, not ours.
        print("write guard (commit gate is repo-scoped):")
        subprocess.run(["git", "init", "-q", proj], capture_output=True)
        vy = os.path.join(proj, ".game_loop", "verify.yaml")
        with open(vy) as f:
            vy_src = f.read()
        with open(vy, "w") as f:
            f.write('"*.txt":\n  - "false"\n')
        with open(os.path.join(proj, "note.txt"), "w") as f:
            f.write("x\n")
        commit = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": proj}
        check("blocks an in-repo commit whose owed checks are stale", denied(guard(proj, commit)))
        elsewhere = tempfile.mkdtemp(prefix="gameloop-other-")
        try:
            check("allows a commit made in a DIFFERENT repo (cwd elsewhere)",
                  allowed(proj, {"tool_name": "Bash",
                                 "tool_input": {"command": "git commit -m x"},
                                 "cwd": elsewhere}))
            check("still blocks a commit targeting this repo via git -C from elsewhere",
                  denied(guard(proj, {"tool_name": "Bash",
                                      "tool_input": {"command": f"git -C {proj} commit -m x"},
                                      "cwd": elsewhere})))
        finally:
            shutil.rmtree(elsewhere, ignore_errors=True)

        # #9: a denied commit that was CHAINED with other work must name that work — the deny stops
        # the WHOLE command body, and retrying just the commit silently drops the chained edits.
        print("write guard (denied commit names chained work):")
        r = guard(proj, {"tool_name": "Bash", "cwd": proj,
                         "tool_input": {"command": "rm -f scratch.txt && git commit -m x"}})
        check("a chained denial lists the segment that never ran",
              denied(r) and "rm -f scratch.txt" in r.stdout and "NONE of them executed" in r.stdout)
        r = guard(proj, {"tool_name": "Bash", "cwd": proj,
                         "tool_input": {"command": "git commit -m x && rm -f after.txt"}})
        check("segments AFTER the commit are lost too, and listed",
              denied(r) and "rm -f after.txt" in r.stdout)
        r = guard(proj, {"tool_name": "Bash", "cwd": proj,
                         "tool_input": {"command": "git commit -m x"}})
        check("a bare denied commit adds no chained-work warning",
              denied(r) and "OTHER OPERATION" not in r.stdout)
        r = guard(proj, {"tool_name": "Bash", "cwd": proj,
                         "tool_input": {"command": "cd . && git commit -m x"}})
        check("a bare cd is navigation, not reported as lost work",
              denied(r) and "OTHER OPERATION" not in r.stdout)
        with open(vy, "w") as f:
            f.write(vy_src)

        # #16: a redirect token must stop at a shell metacharacter. A redirect inside a command
        # substitution used to swallow the closing paren — `2>/dev/null)` is not the sink
        # `/dev/null`, so the discard device denied as if it were an out-of-repo file, and the
        # denial for a genuinely suspicious path named the wrong target. Both directions matter:
        # the sinks must pass, and a real out-of-repo write in the same position must still deny,
        # with a CLEAN path in the message.
        print("write guard (redirect targets stop at shell metacharacters):")
        check("allows the live case: 2>/dev/null inside a $(...) loop header",
              allowed(proj, {"tool_name": "Bash", "tool_input": {
                  "command": 'for source in $(find /a /b -type f -name "*.rs" 2>/dev/null); '
                             'do echo $source; done'}}))
        check("allows >/dev/stdout inside a command substitution",
              allowed(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "echo $(cat file.txt >/dev/stdout)"}}))
        check("allows 2>/dev/tty inside a command substitution",
              allowed(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "echo $(grep -c x file.txt 2>/dev/tty)"}}))
        r = guard(proj, {"tool_name": "Bash", "tool_input": {
            "command": "echo $(cat file.txt > ~/gl_paren_outside.txt)"}})
        check("still denies a real out-of-repo redirect inside a command substitution",
              denied(r) and "gl_paren_outside.txt" in r.stdout)
        # An absence assertion needs something to be absent FROM, or an empty stdout satisfies it
        # forever: this line passed against a guard that had been mutated to check nothing, because
        # nothing is not there either. So require the CLEAN form to be present in the same breath —
        # then "no trailing paren" is a claim about a path the guard actually named.
        check("the denied target carries no trailing paren (the offender is a usable path)",
              "gl_paren_outside.txt" in r.stdout and "gl_paren_outside.txt)" not in r.stdout)
        # #5 must not regress: a QUOTED target after a real redirect is still a genuine write,
        # command substitution or not.
        check("still denies a QUOTED out-of-repo redirect target inside a command substitution",
              denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": 'echo $(cat file.txt > "$HOME/gl_qparen_outside.txt")'}})))

        # #6: stale sessions are pruned on `status` — old + no active mandate goes, an active mandate
        # stays regardless of age, and fresh dirs stay.
        print("session GC:")
        sess_root = os.path.join(proj, ".game_loop", "sessions")
        old = 40 * 86400
        import time as _time
        for name, mandate_active in (("sess-old-idle", False), ("sess-old-live", True)):
            d = os.path.join(sess_root, name)
            os.makedirs(d, exist_ok=True)
            sf = os.path.join(d, "state.json")
            with open(sf, "w") as f:
                json.dump({"mandate": {"active": mandate_active, "text": "x"}}, f)
            os.utime(sf, (_time.time() - old, _time.time() - old))
            os.utime(d, (_time.time() - old, _time.time() - old))
        gl(proj, "status", sid="sess-gc")
        check("prunes an old session with no active mandate",
              not os.path.exists(os.path.join(sess_root, "sess-old-idle")))
        check("never prunes an old session holding an ACTIVE mandate",
              os.path.exists(os.path.join(sess_root, "sess-old-live")))
        check("keeps fresh sessions", os.path.exists(os.path.join(sess_root, "sess-aaa")))
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            check("logs the prune", '"sessions_pruned"' in f.read())

        # A fake Slack: real HTTP, canned responses — notify.py's urllib path is exercised for real,
        # no live workspace involved. `posts` records every (path, body) so tests assert what left.
        print("slack paging (fake server):")
        import http.server
        import threading

        class FakeSlack(http.server.BaseHTTPRequestHandler):
            posts = []
            thread_replies = []
            channel_msgs = []      # top-level channel messages (conversations.history)
            history_error = None   # set to e.g. "missing_scope" to make history reads fail

            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                FakeSlack.posts.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
                self._json({"ok": True, "ts": "111.222"})

            def do_GET(self):
                if self.path.startswith("/conversations.replies"):
                    self._json({"ok": True, "messages": FakeSlack.thread_replies})
                elif self.path.startswith("/conversations.history"):
                    if FakeSlack.history_error:
                        self._json({"ok": False, "error": FakeSlack.history_error})
                    else:
                        self._json({"ok": True, "messages": FakeSlack.channel_msgs})
                else:
                    self._json({"ok": True})

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), FakeSlack)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        api = f"http://127.0.0.1:{srv.server_address[1]}"
        notify_f = os.path.join(proj, ".game_loop", "notify.json")
        with open(notify_f, "w") as f:
            json.dump({"slack": {"bot_token": "xoxb-test", "channel": "C123",
                                 "api_base": api, "reply_poll_sec": 5}}, f)

        r = gl(proj, "notify", "--test")
        check("notify --test pages the channel",
              r.returncode == 0 and "paged" in r.stdout
              and any(p == "/chat.postMessage" for p, _ in FakeSlack.posts))
        check("notify --test verifies reply READS work (history scope present)",
              "reply reads work" in r.stdout)
        # a wrong history scope (channels:history on a PRIVATE channel) must be NAMED, not swallowed
        FakeSlack.history_error = "missing_scope"
        r = gl(proj, "notify", "--test")
        check("notify --test names a missing history scope instead of failing silently",
              "READS FAILED" in r.stdout and "missing_scope" in r.stdout
              and "groups:history" in r.stdout)
        FakeSlack.history_error = None
        gl(proj, "mandate", "--set", "notify work", sid="sess-slk")
        FakeSlack.posts.clear()
        gl(proj, "arm", "--question", "prod or staging?", "--read", real,
           "--predict", "staging", sid="sess-slk")
        with open(os.path.join(proj, ".game_loop", "sessions", "sess-slk", "state.json")) as f:
            slk_state = json.load(f)
        check("arm pages the channel and keeps the thread ts",
              any("prod or staging?" in json.dumps(b) for _, b in FakeSlack.posts)
              and (slk_state.get("t3_armed") or {}).get("slack_ts") == "111.222")

        # The turn-end that ASKS the question runs through the stop gate FIRST. It must not destroy the
        # arm's thread ts, or the watchdog below finds nothing to poll (the real-world bug: reply never
        # forwarded). A Slack-paged arm survives as `spent`; a spent arm no longer re-opens the gate.
        print("a slack-paged arm survives the stop gate (so the reply can still come back):")
        r = gl(proj, "stopgate",
               stdin=json.dumps({"last_assistant_message": "prod or staging — which one?"}),
               sid="sess-slk")
        with open(os.path.join(proj, ".game_loop", "sessions", "sess-slk", "state.json")) as f:
            slk_state = json.load(f)
        check("the ask turn-end passes the gate once (exit 0)", r.returncode == 0)
        check("a slack-paged arm survives the gate (kept spent, ts intact) for the watchdog to poll",
              (slk_state.get("t3_armed") or {}).get("slack_ts") == "111.222"
              and (slk_state.get("t3_armed") or {}).get("spent") is True)
        r = gl(proj, "stopgate",
               stdin=json.dumps({"last_assistant_message": "and which region?"}), sid="sess-slk")
        check("a spent slack arm does not re-open the gate (one interruption holds)", r.returncode == 2)

        print("watchdog forwards a slack reply:")
        FakeSlack.thread_replies = [
            {"ts": "111.222", "text": "parent"},
            {"ts": "111.333", "user": "U1", "text": "staging please"}]
        r = run_watchdog(wd_bin, {"session_id": "sess-slk",
                                                       "transcript_path": tpath},
                         WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0")
        with open(os.path.join(proj, ".game_loop", "sessions", "sess-slk", "state.json")) as f:
            slk_state = json.load(f)
        check("a human thread reply rings the session with the answer (exit 2)",
              r.returncode == 2 and "staging please" in r.stderr)
        check("the forwarded reply clears the arm", not slk_state.get("t3_armed"))
        FakeSlack.posts.clear()
        gl(proj, "mandate", "--clear", "--notes", "done", sid="sess-slk")
        check("mandate --clear pages the channel",
              any("mandate complete" in json.dumps(b) for _, b in FakeSlack.posts))

        # The reply path must work with NO mandate bound — the case Slack paging exists for: mandate
        # finished, agent still has a question, human is away. The watchdog must reach the poller and
        # forward the reply even though the mandate is cleared (regression for the second bug: the
        # watchdog stood down on "no mandate bound" BEFORE ever checking for a Slack-paged arm).
        print("watchdog forwards a slack reply even with NO mandate bound:")
        FakeSlack.posts.clear()
        gl(proj, "arm", "--question", "deploy now?", "--read", real, "--predict", "yes", sid="sess-slk")
        FakeSlack.thread_replies = [
            {"ts": "111.222", "text": "parent"},
            {"ts": "111.555", "user": "U1", "text": "yes deploy it"}]
        r = run_watchdog(wd_bin, {"session_id": "sess-slk",
                                                       "transcript_path": tpath},
                         WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0")
        check("an unmandated session still forwards the slack reply (exit 2)",
              r.returncode == 2 and "yes deploy it" in r.stderr)

        # A human who answers at the CHANNEL top level (not in-thread) must also be caught — the natural
        # thing to do from a phone. replies() unions conversations.history with conversations.replies.
        print("watchdog forwards a top-level channel reply (not just in-thread):")
        FakeSlack.posts.clear()
        FakeSlack.thread_replies = [{"ts": "111.222", "text": "parent"}]   # NO in-thread human reply
        FakeSlack.channel_msgs = [{"ts": "111.777", "user": "U1", "text": "answer in the channel"}]
        gl(proj, "arm", "--question", "channel reply?", "--read", real, "--predict", "x", sid="sess-slk")
        r = run_watchdog(wd_bin, {"session_id": "sess-slk",
                                                       "transcript_path": tpath},
                         WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0")
        check("a top-level channel message is forwarded like a thread reply (exit 2)",
              r.returncode == 2 and "answer in the channel" in r.stderr)
        FakeSlack.channel_msgs = []
        # paging must never take down the verb that pages: point at a dead port and arm anyway
        with open(notify_f, "w") as f:
            json.dump({"slack": {"bot_token": "x", "channel": "C",
                                 "api_base": "http://127.0.0.1:9"}}, f)
        gl(proj, "mandate", "--set", "unreachable slack", sid="sess-slk2")
        r = gl(proj, "arm", "--question", "q?", "--read", real, "--predict", "p", sid="sess-slk2")
        check("an unreachable Slack never breaks a verb (arm still exits 0)", r.returncode == 0)
        gl(proj, "mandate", "--clear", "--notes", "done", sid="sess-slk2")
        with open(notify_f, "w") as f:   # restore the working fake for the limit tests below
            json.dump({"slack": {"bot_token": "xoxb-test", "channel": "C123",
                                 "api_base": api}}, f)

        print("statusline tap:")
        import time as _t
        now_epoch = _t.time()
        sl_payload = {"session_id": "sess-lim",
                      "model": {"display_name": "TestModel"},
                      "context_window": {"used_percentage": 41.0},
                      "rate_limits": {"five_hour": {"used_percentage": 23.5,
                                                    "resets_at": now_epoch + 3600},
                                      "seven_day": {"used_percentage": 41.2,
                                                    "resets_at": now_epoch + 86400}}}
        limits_f = os.path.join(proj, ".game_loop", "limits.json")
        r = gl(proj, "statusline", stdin=json.dumps(sl_payload))
        snap = json.load(open(limits_f))
        check("statusline snapshots rate_limits to limits.json and renders a row",
              r.returncode == 0 and "5h" in r.stdout
              and snap["windows"]["five_hour"]["used_percentage"] == 23.5)
        r = gl(proj, "statusline", stdin='{"model":{"display_name":"X"}}')
        check("statusline stays calm with no rate_limits (API-key auth)",
              r.returncode == 0 and json.load(open(limits_f))["windows"] == {})
        FakeSlack.posts.clear()
        sl_payload["rate_limits"]["five_hour"]["used_percentage"] = 98.5
        gl(proj, "statusline", stdin=json.dumps(sl_payload))
        snap = json.load(open(limits_f))
        check("crossing the threshold stamps crossed_at and pages ONCE",
              snap["windows"]["five_hour"]["crossed_at"]
              and any("usage window" in json.dumps(b) for _, b in FakeSlack.posts))
        FakeSlack.posts.clear()
        sl_payload["session_id"] = "sess-lim-B"   # a SECOND session's statusline, same account window
        gl(proj, "statusline", stdin=json.dumps(sl_payload))
        check("the same window instance does not page twice (even from another session)",
              not FakeSlack.posts)

        print("limit gate:")
        handoff = os.path.join(proj, ".game_loop", "HANDOFF.md")
        bash_rm = {"tool_name": "Bash", "tool_input": {"command": "rm -f x"}}

        def limitgate(payload):
            return gl(proj, "limitgate", stdin=json.dumps(payload))
        check("gate denies ordinary work over the threshold", denied(limitgate(bash_rm)))
        check("gate allows the Write that creates the handoff",
              not denied(limitgate({"tool_name": "Write", "tool_input": {"file_path": handoff}})))
        check("gate allows game_loop verbs while closed",
              not denied(limitgate({"tool_name": "Bash", "tool_input":
                                    {"command": "./.game_loop/bin/game_loop checkpoint --notes x"}})))
        with open(handoff, "w") as f:
            f.write("# handoff\nwhere I was, what's next\n")
        check("a written handoff opens the gate", not denied(limitgate(bash_rm)))
        os.remove(handoff)
        snap["windows"]["five_hour"]["resets_at"] = now_epoch - 5
        snap["windows"]["seven_day"]["used_percentage"] = 10
        json.dump(snap, open(limits_f, "w"))
        check("a window that already reset no longer binds (fail open)",
              not denied(limitgate(bash_rm)))
        os.remove(limits_f)
        check("no snapshot at all means no gate (fail open)", not denied(limitgate(bash_rm)))

        # Handoffs are PER SESSION: concurrent runs each write sessions/<id>/HANDOFF.md, so one
        # run's dying words can never overwrite another's — and one session's handoff must not
        # open the gate for a sibling that hasn't written its own.
        print("limit gate (per-session handoffs):")
        snap["windows"]["five_hour"]["resets_at"] = now_epoch + 3600
        snap["windows"]["five_hour"]["used_percentage"] = 98.5
        json.dump(snap, open(limits_f, "w"))
        p1 = dict(bash_rm, session_id="sess-lg1")
        p2 = dict(bash_rm, session_id="sess-lg2")
        r = limitgate(p1)
        check("the gate names the session's OWN handoff path",
              denied(r) and "sessions/sess-lg1/HANDOFF.md" in r.stdout)
        hp1 = os.path.join(proj, ".game_loop", "sessions", "sess-lg1", "HANDOFF.md")
        os.makedirs(os.path.dirname(hp1), exist_ok=True)
        with open(hp1, "w") as f:
            f.write("# handoff for lg1\n")
        check("a session's handoff opens the gate for THAT session", not denied(limitgate(p1)))
        check("...but not for a sibling session that wrote nothing", denied(limitgate(p2)))
        os.remove(limits_f)

        # A SECOND TRIGGER ON THE SAME GATE. A nearly-exhausted usage window is only one way to run
        # out of road: a session whose context has grown past the cap re-sends that whole context on
        # every remaining call, and over one measured week 80.7% of the account's spend was exactly
        # that. So the same six properties are owed here as above — denies ordinary work, allows the
        # handoff Write, allows game_loop verbs, opens on a written handoff, fails open with no
        # reading, isolates per session — plus the two this trigger adds: the crossing must not move
        # under it, and the auto handoff must not satisfy it.
        #
        # limits.json is GONE at this point, deliberately: every deny below is the context trigger
        # closing the gate entirely on its own, not a usage window doing it quietly.
        print("limit gate (context-size trigger):")
        ctx_cf = os.path.join(proj, ".game_loop", "config.json")

        def ctx_enable(on, threshold=300000):
            c = json.load(open(ctx_cf))
            c["limits"] = {"context": {"enabled": on, "threshold_tokens": threshold}}
            json.dump(c, open(ctx_cf, "w"))

        tctx = os.path.join(proj, "ctx-transcript.jsonl")

        def ctx_record(tokens, sidechain=False):
            """One assistant record shaped like a real transcript's: the call's context is
            input + cache_read + cache_creation, and isSidechain says whose context it is."""
            return {"type": "assistant", "isSidechain": sidechain,
                    "timestamp": datetime.datetime.now(datetime.timezone.utc)
                                         .strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "message": {"usage": {"input_tokens": 2, "cache_creation_input_tokens": 0,
                                          "cache_read_input_tokens": tokens - 2,
                                          "output_tokens": 50}}}

        def turn_end(sid, *recs):
            """A real turn-end: the Stop gate is what reads the transcript and caches the reading,
            so the tests drive it through the hook rather than reaching past it."""
            with open(tctx, "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            return gl(proj, "stopgate",
                      stdin=json.dumps({"session_id": sid, "transcript_path": tctx}))

        def ctx_state(sid):
            with open(os.path.join(proj, ".game_loop", "sessions", sid, "state.json")) as f:
                return json.load(f).get("context_reading") or {}

        ctx_enable(True)
        p_ctx = dict(bash_rm, session_id="sess-ctx")
        hpc = os.path.join(proj, ".game_loop", "sessions", "sess-ctx", "HANDOFF.md")
        turn_end("sess-ctx", ctx_record(120000))
        check("a turn-end records the context reading off the transcript",
              ctx_state("sess-ctx").get("tokens") == 120000
              and ctx_state("sess-ctx").get("crossed_at") is None)
        check("under the cap the gate stays open", not denied(limitgate(p_ctx)))
        turn_end("sess-ctx", ctx_record(350000))
        crossed_1 = ctx_state("sess-ctx").get("crossed_at")
        check("over the cap the crossing is stamped",
              ctx_state("sess-ctx").get("tokens") == 350000 and crossed_1)
        r = limitgate(p_ctx)
        check("gate denies ordinary work over the context cap, with no usage snapshot at all",
              denied(r) and "350K tokens" in r.stdout and "300K cap" in r.stdout)
        check("the context deny sends the run somewhere — it names the successor verb",
              "game_loop successor" in r.stdout)
        # The auto handoff was refreshed by the very turn-end that recorded the crossing, so it is
        # newer than crossed_at and would open a gate that accepted it (#45's failure, ported).
        check("...and the AUTO handoff that same turn-end just wrote does NOT satisfy it",
              os.path.getsize(hpc) > 0 and denied(limitgate(p_ctx)))
        check("gate allows the Write that creates the handoff",
              not denied(limitgate(dict(p_ctx, tool_name="Write",
                                        tool_input={"file_path": hpc}))))
        check("gate allows game_loop verbs while closed on context",
              not denied(limitgate(dict(p_ctx, tool_input={
                  "command": "./.game_loop/bin/game_loop successor"}))))
        with open(hpc, "w") as f:
            f.write("# handing over\nwhere I was, what is next\n")
        check("a hand-written handoff opens the context gate", not denied(limitgate(p_ctx)))
        # THE BAR MUST NOT MOVE. The reading is refreshed every turn-end; if the gate compared the
        # handoff against the newest READING rather than the CROSSING, the handoff written a turn
        # ago would read as stale and the gate could never be satisfied at all.
        turn_end("sess-ctx", ctx_record(360000))
        check("a later reading carries the crossing forward, so the handoff still counts",
              ctx_state("sess-ctx").get("crossed_at") == crossed_1
              and ctx_state("sess-ctx").get("tokens") == 360000
              and not denied(limitgate(p_ctx)))
        os.remove(hpc)
        check("removing the handoff closes it again", denied(limitgate(p_ctx)))
        # Dropping back under is this trigger's analogue of a usage window resetting.
        turn_end("sess-ctx", ctx_record(90000))
        check("dropping back under the cap ends the crossing and opens the gate",
              ctx_state("sess-ctx").get("crossed_at") is None
              and not denied(limitgate(p_ctx)))
        check("one session's context reading never gates a sibling",
              not denied(limitgate(dict(bash_rm, session_id="sess-ctx-B"))))
        check("no reading recorded at all means no gate (fail open)",
              not denied(limitgate(dict(bash_rm, session_id="sess-ctx-C"))))
        turn_end("sess-ctx-D", ctx_record(80000), ctx_record(900000, sidechain=True))
        check("a subagent's sidechain record is not read as the main thread's context",
              ctx_state("sess-ctx-D").get("tokens") == 80000)
        # DEFAULT-OFF is the whole reason this can ship enabled nowhere: the same recorded reading,
        # the same absent handoff, and the gate must not fire for anyone who did not ask for it.
        turn_end("sess-ctx-E", ctx_record(400000))
        p_off = dict(bash_rm, session_id="sess-ctx-E")
        os.remove(os.path.join(proj, ".game_loop", "sessions", "sess-ctx-E", "HANDOFF.md"))
        check("with the trigger on, that reading closes the gate", denied(limitgate(p_off)))
        ctx_enable(False)
        check("with the trigger off, the identical reading gates nothing",
              not denied(limitgate(p_off)))
        ctx_enable(True, threshold=500000)
        check("raising the cap opens the gate on the next call, not the next turn-end",
              not denied(limitgate(p_off)))
        # Left OFF for the rest of the suite. Everything below this point drives stopgate with other
        # fixtures, and a trigger left armed would gate those sessions on a reading they never asked
        # for — the test equivalent of the default this feature deliberately ships with.
        ctx_enable(False)

        print("the successor verb:")
        r = gl(proj, "successor", "--dry-run", sid="sess-succ")
        check("successor refuses when there is nothing to hand over",
              r.returncode != 0 and "no handoff" in (r.stdout + r.stderr))
        hps = os.path.join(proj, ".game_loop", "sessions", "sess-succ", "HANDOFF.md")
        os.makedirs(os.path.dirname(hps), exist_ok=True)
        with open(hps, "w") as f:
            f.write("# handing over\nthe successor reads this\n")
        r = gl(proj, "successor", "--dry-run", sid="sess-succ")
        sid_1 = re.search(r"successor session id : (\S+)", r.stdout)
        check("successor points the next session at THIS session's handoff and mints it an id",
              r.returncode == 0 and os.path.join("sessions", "sess-succ", "HANDOFF.md") in r.stdout
              and "claude --session-id" in r.stdout and sid_1 and len(sid_1.group(1)) == 36)
        check("--dry-run starts nothing", "DRY RUN" in r.stdout)
        r2 = gl(proj, "successor", "--dry-run", sid="sess-succ")
        check("each run mints a NEW id — a successor is never handed a live session's",
              re.search(r"successor session id : (\S+)", r2.stdout).group(1) != sid_1.group(1))
        r = gl(proj, "successor", "--dry-run", "--session-id", "given-id", sid="sess-succ")
        check("...unless one is given (a prewarmed session)", "given-id" in r.stdout)
        # The GATE refuses the generated handoff; this verb accepts it and says so. Different jobs:
        # the gate is asking the agent for its own account, this is the last act of a run that may
        # be out of road, and the generated floor beats starting the successor blind.
        with open(hps, "w") as f:
            f.write("<!-- game_loop:auto -->\n# HANDOFF\n")   # AUTO_HANDOFF_MARK
        r = gl(proj, "successor", "--dry-run", sid="sess-succ")
        check("handing over the GENERATED handoff is allowed, and announced as such",
              r.returncode == 0 and "GENERATED handoff" in r.stdout)
        r = gl(proj, "successor", sid="sess-succ")
        check("off a non-Warp terminal the default resolves to print — it opens nothing itself",
              r.returncode == 0 and "mode                : print" in r.stdout
              and "no Warp detected" in r.stdout)

        # The terminal is READ, not configured (#78). These four cases are the whole switch: the two
        # things detection decides, and the two overrides that exist because detection is blind in a
        # hook (TERM_PROGRAM unset there) and because ~/.warp/ is outside the repo.
        #
        # Every Warp-detected case below is --dry-run on purpose: a test that let the real path run
        # would write ~/.warp/tab_configs/ and open tabs on the machine running the suite.
        def succ_mode(*args, **envx):
            return gl(proj, "successor", "--dry-run", *args, sid="sess-succ", **envx).stdout

        check("under Warp the default resolves to warp-tab with no config at all",
              "mode                : warp-tab — Warp detected"
              in succ_mode(TERM_PROGRAM="WarpTerminal"))
        check("a terminal that is not Warp resolves to print", "mode                : print"
              in succ_mode(TERM_PROGRAM="Apple_Terminal"))

        def succ_cfg(mode):
            c = json.load(open(ctx_cf))
            lim = c.get("limits") or {}
            lim["successor"] = {"mode": mode}
            c["limits"] = lim
            json.dump(c, open(ctx_cf, "w"))

        succ_cfg("print")
        check("mode \"print\" pins the portable floor even under Warp — the opt-out opts out",
              "mode                : print" in succ_mode(TERM_PROGRAM="WarpTerminal"))
        succ_cfg("warp-tab")
        check("mode \"warp-tab\" forces the tab where detection is blind (a hook: no TERM_PROGRAM)",
              "mode                : warp-tab" in succ_mode())
        succ_cfg("nonsense")
        check("an unreadable mode falls back to auto, never to a silent no-op",
              "mode                : warp-tab — Warp detected"
              in succ_mode(TERM_PROGRAM="WarpTerminal"))
        succ_cfg("auto")

        # The tab title. A tab is narrow, so the cap is a REFUSAL — prose guidance did not hold it in
        # the sibling launcher, which shipped `O | push and scope backups` into a truncating tab.
        check("the title defaults to `<R> | successor` — the repo's initial, so a row of tabs reads",
              re.search(r"tab title           : \S \| successor", succ_mode()))
        check("--task names the work inside that convention",
              "| fix auth" in succ_mode("--task", "fix auth"))
        r = gl(proj, "successor", "--dry-run", "--task", "push and scope backups", sid="sess-succ")
        check("--task refuses more than 3 words / 20 chars rather than truncating in the tab",
              r.returncode != 0 and "too long" in (r.stdout + r.stderr))
        r = gl(proj, "successor", "--dry-run", "--task", "a", "--title", "b", sid="sess-succ")
        check("--task and --title are mutually exclusive",
              r.returncode != 0 and "mutually exclusive" in (r.stdout + r.stderr))
        r = gl(proj, "successor", "--dry-run", "--title", "T | {{x}}", sid="sess-succ")
        check("a title carrying {{ is refused — Warp reads it as a parameter placeholder",
              r.returncode != 0 and "{{" in (r.stdout + r.stderr))
        check("--title is uncapped — it is the deliberate override",
              "tab title           : a much longer deliberate title"
              in succ_mode("--title", "a much longer deliberate title"))

        print("watchdog parks at an exhausted limit and rings at reset:")
        gl(proj, "mandate", "--set", "limit park work", sid="sess-park")
        json.dump({"captured_at": _t.time(),
                   "windows": {"five_hour": {"used_percentage": 99.5,
                                             "resets_at": _t.time() + 2}}},
                  open(limits_f, "w"))
        FakeSlack.posts.clear()
        r = run_watchdog(wd_bin, {"session_id": "sess-park",
                                                       "transcript_path": tpath},
                         WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0",
                                    WATCHDOG_RESUME_BUFFER_SEC="0")
        with open(os.path.join(proj, ".game_loop", "sessions", "sess-park", "state.json")) as f:
            park_state = json.load(f)
        check("exhausted window → park, then ring at reset (exit 2)",
              r.returncode == 2 and "usage window" in r.stderr and "reset" in r.stderr)
        check("the resume ring starts a fresh ring budget",
              park_state.get("watchdog_rings") == 0)
        check("park and resume both page the channel",
              any("parked" in json.dumps(b) for _, b in FakeSlack.posts)
              and any("reset" in json.dumps(b) for _, b in FakeSlack.posts))
        gl(proj, "mandate", "--clear", "--notes", "done", sid="sess-park")
        os.remove(limits_f)
        os.remove(notify_f)
        srv.shutdown()

        print("flair:")
        # a claim emits a fun line; a milestone (10 claims) fires a shout-out
        r = gl(proj, "claim", "--assert", "x", "--read", real)
        check("a claim prints a flair line", "🎮" in r.stdout)
        for _ in range(12):                            # cross the 10-claim milestone
            gl(proj, "claim", "--assert", "x", "--read", real)

        def fired(p):
            return json.load(open(os.path.join(p, ".game_loop", "state.json"))).get("flair_fired", [])
        check("10-claim milestone fires exactly once", fired(proj).count("claim:10") == 1)
        gl(proj, "status"); gl(proj, "status")
        check("a fired milestone does not repeat", fired(proj).count("claim:10") == 1)
        # sponsor CTAs rotate — bind a mandate backdated far enough to cross many uptime milestones
        import datetime as _dt
        gl(proj, "mandate", "--set", "long run")
        st = json.load(open(os.path.join(proj, ".game_loop", "state.json")))
        st["mandate"]["since"] = (_dt.datetime.now() - _dt.timedelta(hours=200)).isoformat(timespec="seconds")
        json.dump(st, open(os.path.join(proj, ".game_loop", "state.json"), "w"))
        r = gl(proj, "status")
        ctas = [ln for ln in r.stdout.splitlines() if "📺" in ln]
        check("many uptime milestones fire with a sponsor CTA", len(ctas) >= 5)
        check("the sponsor CTA wording varies (not all identical)", len(set(ctas)) >= 2)
        # flair.enabled=false silences it
        c = json.load(open(cf)); c["flair"] = {"enabled": False}; json.dump(c, open(cf, "w"))
        r = gl(proj, "claim", "--assert", "x", "--read", real)
        check("flair.enabled=false silences flair", "🎮" not in r.stdout)
        c = json.load(open(cf)); c.pop("flair", None); json.dump(c, open(cf, "w"))

        # Update check: status compares .game_loop/VERSION (installed sha) against the latest sha on the
        # source repo's main, served here by a fake GitHub. Network is real (urllib), workspace is not.
        print("update check (fake github):")

        class FakeGH(http.server.BaseHTTPRequestHandler):
            sha = "a" * 40
            behaviour = None            # set per-assertion; None = 404, i.e. "could not fetch"
            compare = "behind"          # where the INSTALLED sha sits relative to latest (#49)
            def do_GET(self):
                if "/compare/" in self.path:
                    if FakeGH.compare is None:
                        self.send_response(500)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    body = json.dumps({"status": FakeGH.compare}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if "behaviour.json" in self.path:
                    if FakeGH.behaviour is None:
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    body = FakeGH.behaviour.encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps({"sha": FakeGH.sha}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a):
                pass

        ghsrv = http.server.HTTPServer(("127.0.0.1", 0), FakeGH)
        threading.Thread(target=ghsrv.serve_forever, daemon=True).start()
        ghbase = f"http://127.0.0.1:{ghsrv.server_address[1]}"
        ver_f = os.path.join(proj, ".game_loop", "VERSION")
        cache_f = os.path.join(proj, ".game_loop", ".update_cache.json")
        cf_orig = open(cf).read()

        def set_cfg(**extra):
            base = {"project_name": "t", "update_repo": "me/gl", "update_api_base": ghbase}
            base.update(extra)
            json.dump(base, open(cf, "w"))

        def fresh_status():
            if os.path.exists(cache_f):
                os.remove(cache_f)                 # force a live lookup, don't ride a stale cache
            return gl(proj, "status").stdout

        set_cfg()
        open(ver_f, "w").write("b" * 40 + "\n")
        check("status flags an available update when installed sha != latest",
              "update available" in fresh_status() and "aaaaaaaa" in fresh_status())

        # A CORRECT ALERT WITH A DESTRUCTIVE FIX ATTACHED. Reported by a consumer who nearly ran it:
        # the notice told every install to re-run the curl installer, which for a payload placed by
        # a package manager overwrites a blessed, stamped release with whatever is on main at that
        # instant, drops CONFIDENCE, and reverts the integration they had just finished. Detection
        # right, remedy wrong for half the population — and the distinguishing fact is on disk.
        _by_f = os.path.join(proj, ".game_loop", "installed-by.json")
        _plain = fresh_status()
        check("...and with no packager marker it still offers the curl, but SAYS what that does — "
              "installing whatever is on main right now, which may be ahead of any blessed commit",
              "curl -fsSL" in _plain and "AT THAT INSTANT" in _plain)
        with open(_by_f, "w") as f:
            json.dump({"name": "examplepkg", "upgrade": "examplepkg upgrade game_loop"}, f)
        _vend = fresh_status()
        check("...while a payload a packager placed is told to upgrade THROUGH it, and the curl is "
              "not offered at all — the remedy comes from the same place as the install",
              "examplepkg upgrade game_loop" in _vend and "curl -fsSL" not in _vend)
        # THE COMMAND IS PRINTED, NEVER RUN — a file game_loop executed would be a code-execution
        # vector wearing a helpful face. Garbage must degrade to the ordinary notice, not to a crash
        # and not to a half-quoted command.
        for _bad in ('{"name": "x", "upgrade": "a\\nb"}', "not json at all", '["upgrade"]'):
            with open(_by_f, "w") as f:
                f.write(_bad)
            _b = fresh_status()
            check(f"...and an unusable marker ({_bad[:18]}…) falls back to the ordinary notice "
                  "rather than printing a command nobody can trust",
                  "update available" in _b and "curl -fsSL" in _b)
        os.remove(_by_f)
        open(ver_f, "w").write(FakeGH.sha + "\n")
        check("status is silent when installed sha == latest",
              "update available" not in fresh_status())
        set_cfg(update_check=False)
        open(ver_f, "w").write("c" * 40 + "\n")
        check("update_check:false silences the notice",
              "update available" not in fresh_status())
        set_cfg(update_api_base="http://127.0.0.1:9")   # dead port — must never be reached
        os.remove(ver_f)
        check("no VERSION file → update check stays silent (and hits no network)",
              "update available" not in fresh_status())
        # #49 — INEQUALITY IS NOT STALENESS. "installed != latest" is true both when the install
        # is behind (a real update) and when it is AHEAD of a cached check, and those are opposite
        # conditions. A tree updated twice in one session was told it was behind, and the remedy it
        # was given was a no-op that left the notice in place.
        set_cfg()
        open(ver_f, "w").write("b" * 40 + "\n")
        FakeGH.compare = "ahead"
        check("an install AHEAD of the cached latest is not told to update — it is not behind",
              "update available" not in fresh_status())
        FakeGH.compare = "identical"
        check("...nor is one that is identical by ancestry though the shas were compared unequal",
              "update available" not in fresh_status())
        # PAIRED: the real case must still fire, or the fix above is just a mute button.
        FakeGH.compare = "behind"
        check("...while an install genuinely behind still gets the notice",
              "update available" in fresh_status())
        # DIVERGED IS NOT "BEHIND". It means the installed commit is on no branch — a rebase or a
        # force-push orphaned it — and every other signal a consumer can see says fine: the payload
        # is complete, VERSION is stamped, CONFIDENCE may say stable. Reporting that as an ordinary
        # available update understates it to the one party who cannot check.
        #
        # I CAUSED ONE. A channel pointer went out at a commit whose branch push had been rejected,
        # so `stable` briefly named a tree on no branch and my own rebase orphaned it. The
        # producer-side guard only helps whoever installs AFTERWARDS; anyone inside the window
        # depended on me noticing and writing to them. A downstream maintainer named that as the
        # half that cannot see anything, and this is that half.
        FakeGH.compare = "diverged"
        _div = fresh_status()
        check("a DIVERGED install is told its commit is not on the branch at all, not merely that "
              "an update exists — the one failure a consumer cannot otherwise detect",
              "NOT ON" in _div.upper() and "orphaned" in _div)
        check("...and it names the reason everything else looks fine, so the reader does not "
              "dismiss it as the usual nag",
              "payload" in _div and "VERSION is stamped" in _div)
        check("...and offers a ref a branch actually contains, rather than leaving them to guess",
              "GAME_LOOP_CHANNEL=stable" in _div)
        # COULD NOT DETERMINE is a third answer, and it must not be spoken as either of the others.
        FakeGH.compare = None
        _undet = fresh_status()
        check("an undeterminable comparison says the direction is unknown and implies no re-install",
              "could NOT be determined" in _undet and "update available" not in _undet)
        FakeGH.compare = "behind"

        # The SHIPPED record is data the notice reads, and a malformed one degrades to silence: the
        # notice reports nothing and looks exactly like an install with nothing to report. So the
        # file itself is checked, not just the parser.
        with open(os.path.join(REPO, ".game_loop", "behaviour.json")) as f:
            _shipped = f.read()
        _entries = sweep_free_behaviour = json.loads(_shipped).get("changes")
        check("the shipped behaviour record parses and every entry carries seq, verb and change",
              isinstance(_entries, list) and _entries
              and all(isinstance(e.get("seq"), int) and str(e.get("verb", "")).strip()
                      and str(e.get("change", "")).strip() for e in _entries))
        check("...and its seqs are unique and ascending — seq IS the ordering, since shas have none",
              [e["seq"] for e in _entries] == sorted({e["seq"] for e in _entries}))

        # #37 — WHAT changed, not merely THAT something did. "An update is available" is true and
        # useless for deciding whether to care. A new verb costs nothing until you reach for it; an
        # existing verb that starts costing minutes, or refusing where it used to warn, changes
        # behaviour for somebody who asked for nothing. Only that earns the interruption.
        _bhv_f = os.path.join(proj, ".game_loop", "behaviour.json")
        _rec = lambda *seqs: json.dumps({"changes": [
            {"seq": n, "verb": f"v{n}", "change": f"c{n}"} for n in seqs]})
        set_cfg(update_raw_base=ghbase)
        open(ver_f, "w").write("b" * 40 + "\n")
        FakeGH.behaviour = _rec(1, 2)

        with open(_bhv_f, "w") as f:
            f.write(_rec(1))
        _one = fresh_status()
        check("an update names the behaviour changes this install has NOT seen, and only those",
              "BEHAVIOUR CHANGE" in _one and "v2: c2" in _one and "v1: c1" not in _one)

        # PAIRED: caught up must be SILENT about behaviour, or every update carries the same
        # paragraph forever and the one that matters is the one nobody reads.
        with open(_bhv_f, "w") as f:
            f.write(_rec(1, 2))
        _caught = fresh_status()
        check("...and an install already carrying every entry says nothing about behaviour at all",
              "update available" in _caught and "BEHAVIOUR CHANGE" not in _caught)

        # An install predating the record has seen NONE of them — 0, not "unknown, assume fine".
        os.remove(_bhv_f)
        check("an install from before the record existed is told about all of them",
              "v1: c1" in fresh_status() and "v2: c2" in fresh_status())

        # COULD NOT FETCH must never read as NOTHING CHANGED. This is the silence family again:
        # an absent answer and a clean answer are different, and only one of them is reassuring.
        FakeGH.behaviour = None
        _unfetched = fresh_status()
        check("an unreachable record leaves the plain update notice, claiming nothing either way",
              "update available" in _unfetched and "BEHAVIOUR CHANGE" not in _unfetched)
        FakeGH.behaviour = "}not json at all{"
        check("...and so does a malformed one — garbage is no information, not a clean bill",
              "BEHAVIOUR CHANGE" not in fresh_status())

        # An install is the thing that INVALIDATES the update cache, so it must not leave it: the
        # cached `latest` predates the sha just stamped, and comparing them is wrong for the whole
        # TTL — exactly the window in which somebody runs status to confirm the update worked.
        _inst_src = open(os.path.join(REPO, "install.sh")).read()
        check("install.sh removes the update cache it invalidates by definition",
              'rm -f "$TARGET/.game_loop/.update_cache.json"' in _inst_src)

        ghsrv.shutdown()
        open(cf, "w").write(cf_orig)

        # #20: committed-but-unpushed work is invisible to everyone except the agent that wrote it,
        # and the agent reports it as done — locally it IS done. checkpoint and mandate --clear say
        # how far ahead of its upstream HEAD is. A warning, never a block; silent with no upstream.
        # Needs a real repo with a real remote, so this runs in its own sandbox.
        print("unpushed warning:")
        up = make_sandbox()
        remote = tempfile.mkdtemp(prefix="gameloop-remote-")
        try:
            def ug(*args):
                return subprocess.run(["git", "-c", "user.email=t@example.invalid",
                                       "-c", "user.name=tester", "-c", "commit.gpgsign=false",
                                       *args], cwd=up, capture_output=True, text=True)

            def ucommit(name):
                with open(os.path.join(up, name), "w") as f:
                    f.write(name)
                ug("add", "-A")
                ug("commit", "-q", "-m", name)

            ug("init", "-q")
            ucommit("a.txt")
            subprocess.run(["git", "init", "--bare", "-q", remote], capture_output=True)
            ug("remote", "add", "origin", remote)
            ug("push", "-q", "-u", "origin", "HEAD")
            # #42: a warner that has stopped working is quiet for an up-to-date branch, quiet for a
            # branch with no upstream, and quiet for everything else — "stays quiet" and "is broken"
            # are the same observation. So every silence asserted below is a DIFFERENTIAL against a
            # run of the same warner, in this same sandbox, that was seen to SPEAK. (fix_warning's
            # handback checks further down were already built this way; these were not.)
            synced = gl(up, "checkpoint", "--notes", "still on the parser")
            check("up to date with upstream → checkpoint stays quiet",
                  synced.returncode == 0 and "UNPUSHED" not in synced.stdout)
            for f in ("b.txt", "c.txt", "d.txt"):
                ucommit(f)
            r = gl(up, "checkpoint", "--notes", "still on the parser")
            check("ahead of upstream → checkpoint warns, and says by how much",
                  r.returncode == 0 and "UNPUSHED" in r.stdout and "3 commits" in r.stdout)
            check("...so the up-to-date run's silence was a verdict, not a warner that never fires",
                  "UNPUSHED" in r.stdout and "UNPUSHED" not in synced.stdout)
            check("the unpushed warning never blocks the checkpoint",
                  "UNPUSHED" in r.stdout and "✓ CHECKPOINT" in r.stdout)
            gl(up, "mandate", "--set", "land the parser")
            r = gl(up, "mandate", "--clear", "--notes", "parser landed")
            check("mandate --clear warns about unpushed work too",
                  r.returncode == 0 and "UNPUSHED" in r.stdout and "3 commits" in r.stdout)
            plain = gl(up, "checkpoint", "--notes", "mid-way through the parser").stdout
            loud = gl(up, "checkpoint", "--notes", "all done — deploying for the team").stdout
            check("ordinary notes get the plain wording",
                  "UNPUSHED" in plain and "never pushed" not in plain)
            check("handoff-flavoured notes escalate the wording",
                  "UNPUSHED" in loud and "never pushed" in loud)
            # a branch nobody tracks was never promised to anyone: the honest exception
            ug("checkout", "-q", "-b", "side-quest")
            ucommit("e.txt")
            r = gl(up, "checkpoint", "--notes", "all done — deploying for the team")
            check("a branch with NO upstream stays quiet",
                  r.returncode == 0 and "UNPUSHED" not in r.stdout)
            # ...and its control, on the SAME branch and the same notes: give it an upstream, put a
            # commit past it, and the warner has to speak. Without this, the silence above is
            # satisfied for free by a warner that was never going to say anything at all.
            ug("push", "-q", "--set-upstream", "origin", "side-quest")
            ucommit("f.txt")
            tracked = gl(up, "checkpoint", "--notes", "all done — deploying for the team")
            check("...and the same branch, once tracked, warns — the silence was the missing upstream",
                  "UNPUSHED" in tracked.stdout and "UNPUSHED" not in r.stdout)
        finally:
            shutil.rmtree(up, ignore_errors=True)
            shutil.rmtree(remote, ignore_errors=True)
        # #18: environment state the build depends on is invisible to the harness, so a run cannot
        # tell a stale leftover from a load-bearing pin — and "revert the unexplained local state" is
        # normally correct hygiene. A pin carries the fact WITH its reason in resume state, so status
        # (the compaction-recovery path) shows it and reverting it becomes a visible decision.
        # Every rejection below asserts the game_loop die() prefix, not merely a non-zero exit: an
        # unimplemented subcommand also exits non-zero, and a test that passes for that reason is a
        # test that cannot fail.
        print("environment pins:")
        dep = os.path.join(proj, "dep-checkout")
        os.makedirs(dep, exist_ok=True)
        head_f = os.path.join(dep, "HEAD")      # stand-in for a checkout's .git/HEAD
        with open(head_f, "w") as f:
            f.write("abc123def456\n")
        pinned = ["pin", "--fact", "dep-checkout is pinned to abc123def456",
                  "--reason", "the merge API only exists on that commit; default branch lacks it",
                  "--path", head_f, "--expect", "abc123def456",
                  "--restore", "git -C dep-checkout checkout abc123def456"]
        r = gl(proj, *pinned)
        check("registers a pin anchored to a real path", r.returncode == 0 and "PIN" in r.stdout)
        r = gl(proj, "status")
        check("the pin survives into status, with the reason it is load-bearing",
              "dep-checkout is pinned to abc123def456" in r.stdout
              and "the merge API only exists on that commit" in r.stdout
              and "git -C dep-checkout checkout abc123def456" in r.stdout)
        check("pin --list shows the live pin", "abc123def456" in gl(proj, "pin", "--list").stdout)
        r = gl(proj, "pin", "--fact", "toolchain is 3.11", "--reason", "the build needs it",
               "--path", os.path.join(proj, "no-such-checkout"))
        check("refuses a pin whose --path names nothing on disk",
              r.returncode != 0 and "GAMELOOP ✗" in r.stderr)
        r = gl(proj, "pin", "--fact", "toolchain is 3.11", "--path", head_f)
        check("refuses a pin with no --reason (an unexplained pin is the invisible state again)",
              r.returncode != 0 and "GAMELOOP ✗" in r.stderr)
        r = gl(proj, "pin", "--fact", "dep is at deadbeef", "--reason", "why",
               "--path", head_f, "--expect", "deadbeef")
        check("refuses an --expect that does not already hold (a check born red is no check)",
              r.returncode != 0 and "GAMELOOP ✗" in r.stderr)
        # The reported failure exactly: the anchor still EXISTS after the tidy-up, its content moved.
        with open(head_f, "w") as f:
            f.write("ref: refs/heads/main\n")
        check("status flags a pin whose anchor drifted out from under it",
              "DRIFTED" in gl(proj, "status").stdout)
        with open(head_f, "w") as f:
            f.write("abc123def456\n")
        # #31 — INVERTED, deliberately, and this assertion used to say the opposite. #18 shipped
        # pins session-scoped, and the incident it was built from says why that was wrong: the
        # damage happens "later, cleaning up loose ends" — a different moment, very often a
        # different session, arriving fresh with no memory of why the state was unusual. Scoped to
        # one session, the guard protected the only run that did not need protecting.
        #
        # #17 answered the same question for refuted claims the other way and wrote down why: a
        # negative result is knowledge about the CHECKOUT. That argument is stronger for a pin,
        # which is knowledge about the ENVIRONMENT two sessions genuinely share.
        gl(proj, "pin", "--fact", "sess-pin-A only", "--reason", "A's build needs it",
           "--path", head_f, sid="pinA0001")
        _other = gl(proj, "status", sid="pinB0002").stdout
        check("a pin registered in one session DOES reach another's status — the run that tidies "
              "it away is the one that never knew why it was there",
              "sess-pin-A only" in _other
              and "sess-pin-A only" in gl(proj, "status", sid="pinA0001").stdout)
        check("...and the other session is told WHO pinned it, so there is somebody to ask",
              "pinned by" in _other)
        # The effector/instrument/fix registries stay session-scoped on purpose: a proof that a verb
        # acts, or that a metric is controlled, is a PERISHABLE fact about this run's regime, and
        # inheriting one is the "assumed to have survived the change" failure. Asserted here so the
        # sharing above reads as a decision about pins rather than a drift toward sharing everything.
        gl(proj, "effector", "--prove", "scroll31", "--known-state", "k",
           "--before", head_f, "--observed", head_f, sid="pinA0001")
        check("...while an effector proof stays SESSION-scoped — perishable facts are not shared",
              "scroll31" not in gl(proj, "status", sid="pinB0002").stdout)
        r = gl(proj, "pin", "--release", "p1")
        check("refuses to release a pin without --notes (the revert must stay a stated decision)",
              r.returncode != 0 and "GAMELOOP ✗" in r.stderr)
        r = gl(proj, "pin", "--release", "p1", "--notes", "the API landed on the default branch")
        check("releases a pin by id", r.returncode == 0 and "RELEASED" in r.stdout)
        r = gl(proj, "status")   # a bare absence would pass against an unimplemented verb, so this
        check("a released pin is gone from status, while pins nobody released stay — the list is "
              "the CHECKOUT's now, so 'none left' is no longer the same claim as 'this one went'",
              "dep-checkout is pinned to abc123def456" not in r.stdout
              and "sess-pin-A only" in r.stdout)
        r2 = gl(proj, "pin", "--release", "p2", "--notes", "A's build moved on")
        check("...and releasing another session's pin SAYS whose it was — the run tidying up is by "
              "construction the one that never knew why the state was unusual",
              r2.returncode == 0 and "registered by ANOTHER session" in r2.stdout)
        check("...and with every pin released the empty-pins line is back",
              "pins: none" in gl(proj, "status").stdout)
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            log = f.read()
        check("both the pin and its release are permanent in the log",
              '"pin"' in log and '"pin_release"' in log
              and "the API landed on the default branch" in log)
        # #15: a HUMAN-called break needs somewhere to go. Without it the only endings are "violate
        # the gate" or "fabricate a closure that reads forever as though the work was done". A park
        # keeps the mandate OPEN and attributes the break to the human — and, crucially, it is
        # SINGLE-USE, so it cannot be spent to disarm the gate permanently.
        print("mandate park (a human-called break, #15):")
        pk = "sess-park15"
        gl(proj, "mandate", "--set", "ship the outcomes work", sid=pk)
        r = gl(proj, "mandate", "--park", sid=pk)
        check("park refuses without the human's words (--reason)",
              r.returncode != 0 and "reason" in r.stderr)
        r = gl(proj, "mandate", "--park", "--reason", "take a break, I need the branch",
               "--next", "re-run the control experiment", sid=pk)
        check("park with the human's verbatim words is accepted", r.returncode == 0)
        r = gl(proj, "stopgate", stdin=json.dumps(
            {"session_id": pk, "last_assistant_message": "Parked. Everything is committed."}))
        check("a parked mandate lets the turn end (exit 0)", r.returncode == 0)
        r = gl(proj, "stopgate", stdin=json.dumps(
            {"session_id": pk, "last_assistant_message": "Still stopped."}))
        check("a park buys ONE turn-end, then the gate is live again (exit 2)",
              r.returncode == 2 and "PARKED" in r.stderr)
        r = gl(proj, "status", sid=pk)
        check("a parked mandate still shows as OPEN work on resume",
              "PARKED" in r.stdout and "ship the outcomes work" in r.stdout)
        check("the human's verbatim words survive into status",
              "take a break, I need the branch" in r.stdout)
        check("the recorded next step survives into status",
              "re-run the control experiment" in r.stdout)
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            log = f.read()
        check("the log distinguishes a human-called break from a self-terminated run",
              '"kind": "mandate_park"' in log and '"by": "human"' in log)
        # the word `park` is already taken by the usage-limit park (watchdog). Both must coexist:
        # different noun parked, different log kind, neither shadowing the other.
        check("the usage-limit park is untouched — both park kinds appear, distinctly",
              '"kind": "watchdog_limit_park"' in log and '"kind": "mandate_park"' in log)
        rp = gl(proj, "mandate", "--park", "--reason", "back in an hour", sid=pk)
        r = gl(proj, "stopgate", stdin=json.dumps(
            {"session_id": pk, "last_assistant_message": "Parked. Should I switch branches first?"}))
        check("a park does not launder a question at turn-end",
              rp.returncode == 0 and r.returncode == 2)
        rr = gl(proj, "mandate", "--resume", sid=pk)
        check("resume hands the break's own words back",
              rr.returncode == 0 and "back in an hour" in rr.stdout)
        r = gl(proj, "status", sid=pk)
        check("a resumed mandate is live again, no longer parked",
              rr.returncode == 0 and "PARKED" not in r.stdout
              and "MANDATE: ship the outcomes work" in r.stdout)
        r = gl(proj, "stopgate", stdin=json.dumps(
            {"session_id": pk, "last_assistant_message": "Ending here."}))
        check("after resume the gate is live again (bare turn-end blocks)",
              rr.returncode == 0 and r.returncode == 2)
        # the break must actually happen: a watchdog that rings a parked run drags the session back
        # to work the human paused.
        gl(proj, "mandate", "--park", "--reason", "stepping out", sid=pk)
        r = run_watchdog(wd_bin, {"session_id": pk, "transcript_path": tpath},
                         WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0")
        check("the watchdog does not ring a run parked by its human", r.returncode == 0)
        gl(proj, "mandate", "--clear", "--notes", "done", sid=pk)

        # #17: being wrong is the outcome most worth keeping. A retraction must be structurally
        # distinct from a success, must carry the control that killed it, and must cost no more than
        # any other claim — the incentive to quietly move on is the bug being fixed.
        print("claim outcomes (being wrong is first-class, #17):")
        r = gl(proj, "claim", "--assert", "the cache is invalidated on write",
               "--read", real, "--outcome", "refuted")
        check("a refuted claim REFUSES without the disproving evidence",
              r.returncode != 0 and "--evidence" in r.stderr)
        r = gl(proj, "claim", "--assert", "the cache is invalidated on write", "--read", real,
               "--outcome", "refuted", "--evidence", "/no/such/control.log")
        check("a refuted claim refuses evidence that isn't a real file",
              r.returncode != 0 and "don't resolve to a real" in r.stderr)
        control = os.path.join(proj, "control.log")
        with open(control, "w") as f:
            f.write("control run: the cache was NOT invalidated\n")
        r = gl(proj, "claim", "--assert", "the cache is invalidated on write", "--read", real,
               "--outcome", "refuted", "--evidence", control)
        check("a refuted claim with real evidence is recorded",
              r.returncode == 0 and "REFUTED" in r.stdout)
        r = gl(proj, "claim", "--assert", "x", "--read", real, "--outcome", "nonsense")
        check("an unknown --outcome is refused",
              r.returncode != 0 and "inconclusive" in r.stderr)
        r = gl(proj, "claim", "--assert", "the flag defaults on", "--outcome", "refuted",
               "--evidence", control)
        check("a retraction costs exactly one real path (evidence stands in for --read)",
              r.returncode == 0)
        r = gl(proj, "status")
        check("status surfaces the standing RULED-OUT list",
              "RULED OUT" in r.stdout and "the cache is invalidated on write" in r.stdout)
        check("the ruled-out entry names the control that killed it", "control.log" in r.stdout)
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            log = f.read()
        check("a refutation is greppable in the log as an outcome, not a success",
              '"outcome": "refuted"' in log)

        # #19: an EFFECTOR is a verb the run ACTS with, and one that fails quietly does not produce
        # zero findings — it produces FALSE ones, indistinguishable in tone and detail from real
        # ones. Every incident behind this gate EXITED ZERO, so the gate must be unsatisfiable by a
        # return code: the keystone is a before/after pair THIS TOOL compares, never an assertion
        # that something moved.
        print("effector proofs (a verb that actually acts, #19):")
        cap = os.path.join(proj, "captures")
        os.makedirs(cap, exist_ok=True)
        before = os.path.join(cap, "before.txt")
        after = os.path.join(cap, "after.txt")
        unchanged = os.path.join(cap, "unchanged.txt")
        with open(before, "w") as f:
            f.write("comps: row1\ncomps: row2\n")
        with open(unchanged, "w") as f:
            f.write("comps: row1\ncomps: row2\n")   # byte-identical: `cliclick w:` waited, exit 0
        with open(after, "w") as f:
            f.write("comps: row1\ncomps: row2\ncomps: row3 BELOW THE FOLD\n")
        r = gl(proj, "effector", "--prove", "scroll", "--known-state", "the comps list overflows",
               "--exit-code", "0")
        check("a proof backed only by an exit code is refused, by name",
              r.returncode != 0 and "EXIT CODE IS NOT THE ASSERTION" in r.stderr)
        r = gl(proj, "effector", "--prove", "scroll", "--known-state", "the comps list overflows",
               "--observed", after)
        check("a result with nothing to compare it against is refused",
              r.returncode != 0 and "GAMELOOP ✗" in r.stderr and "--before" in r.stderr)
        r = gl(proj, "effector", "--prove", "scroll", "--before", before, "--observed", after)
        check("a proof without a known-response state is refused",
              r.returncode != 0 and "--known-state" in r.stderr)
        r = gl(proj, "effector", "--prove", "scroll", "--known-state", "the comps list overflows",
               "--before", before, "--observed", "/no/such/capture.png")
        check("a proof whose observed artifact does not resolve is refused",
              r.returncode != 0 and "--observed does not resolve" in r.stderr)
        r = gl(proj, "effector", "--prove", "scroll", "--known-state", "the comps list overflows",
               "--before", "/no/such/capture.png", "--observed", after)
        check("a proof whose before artifact does not resolve is refused",
              r.returncode != 0 and "--before does not resolve" in r.stderr)
        # THE keystone. This is the `cliclick w:` incident exactly: exit 0, nothing scrolled, and
        # "the app cannot scroll at all" filed as the run's top-severity finding.
        r = gl(proj, "effector", "--prove", "scroll", "--known-state", "the comps list overflows",
               "--before", before, "--observed", unchanged)
        check("an identical before/after pair is refused — the world did not move",
              r.returncode != 0 and "IDENTICAL" in r.stderr)
        r = gl(proj, "effector", "--prove", "scroll", "--known-state", "the comps list overflows",
               "--before", before, "--observed", after, "--expect", "row9 NEVER RENDERED")
        check("--expect absent from the observed capture is refused (something changed, not THAT)",
              r.returncode != 0 and "does not appear in the observed capture" in r.stderr)
        r = gl(proj, "effector", "--prove", "scroll", "--known-state", "the comps list overflows",
               "--before", before, "--observed", after, "--expect", "comps: row1")
        check("--expect that was ALREADY true before the act is refused",
              r.returncode != 0 and "ALREADY in the before capture" in r.stderr)
        r = gl(proj, "effector", "--prove", "scroll", "--known-state",
               "the comps list overflows the fold", "--before", before, "--observed", after,
               "--expect", "row3 BELOW THE FOLD", "--scale", "1.73")
        check("a proof naming a known-response state and a real observed change is accepted",
              r.returncode == 0 and "EFFECTOR PROVED" in r.stdout)
        check("an accepted proof states what it does NOT catch (INV6)",
              "DOES NOT CATCH" in r.stdout and "point-in-time" in r.stdout)
        r = gl(proj, "claim", "--assert", "the app cannot scroll at all", "--read", real,
               "--effector", "click")
        check("a claim depending on an unproven effector is refused",
              r.returncode != 0 and "nothing in this session proves it did" in r.stderr)
        r = gl(proj, "claim", "--assert", "the comps table renders below the fold",
               "--effector", "scroll")
        check("a claim on a proved effector is admitted, its pair standing in for --read",
              r.returncode == 0 and "effector  : scroll" in r.stdout)
        # A proof is a perishable fact about THIS run's environment (this display awake, this helper
        # wired to this binary), so it must not admit a sibling session's findings.
        r = gl(proj, "claim", "--assert", "the comps table renders below the fold",
               "--effector", "scroll", sid="sess-eff-b")
        check("an effector proof does not leak into another session's admissions",
              r.returncode != 0 and "nothing in this session proves it did" in r.stderr)
        # "Arithmetic in the harness is a defect generator": 640x1.73 was the conversion whose
        # author got it wrong on the very next run, so the tool does it.
        r = gl(proj, "effector", "--aim", "scroll", "--at", "640,480")
        check("the tool converts coordinates so the caller never multiplies by hand",
              r.returncode == 0 and "1107,830" in r.stdout)
        r = gl(proj, "effector", "--aim", "nosuch", "--at", "1,1")
        check("aiming an unproven effector is refused (arithmetic on a guess)",
              r.returncode != 0 and "nothing to aim" in r.stderr)
        r = gl(proj, "status")
        check("status carries the proof and its known-response state through compaction",
              "EFFECTORS" in r.stdout and "the comps list overflows the fold" in r.stdout)
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            log = f.read()
        check("the proof is greppable in the log as a compared PAIR, not a verdict",
              '"kind": "effector_proof"' in log and '"before_digest"' in log
              and '"observed_digest"' in log)
        r = gl(proj, "effector", "--release", "scroll")
        check("refuses to retire a proof without --notes (findings were admitted on it)",
              r.returncode != 0 and "--notes" in r.stderr)
        r = gl(proj, "effector", "--release", "scroll", "--notes", "the display slept mid-run")
        check("releases a proof by name", r.returncode == 0 and "RELEASED" in r.stdout)
        r = gl(proj, "claim", "--assert", "the comps table renders below the fold",
               "--effector", "scroll")
        check("after a release, a claim leaning on that effector is refused again",
              r.returncode != 0 and "nothing in this session proves it did" in r.stderr)
        # #22: "only X" / "X is restricted" is a claim about a SET drawn from a sample of one. The
        # observation is usually right and the SCOPE is the invented part — so a second probe on a
        # DIFFERENT member is the price of asserting one, and a repeat of the first probe is not it.
        print("claim scope (a claim about a set costs a second probe, #22):")
        r = gl(proj, "claim", "--assert", "only the events table refuses deletes", "--read", real,
               "--scope", "tables you can delete from", "--probe", "events")
        check("a scope claim with ONE probe is refused",
              r.returncode != 0 and "needs two --probe values" in r.stderr)
        r = gl(proj, "claim", "--assert", "only the events table refuses deletes", "--read", real,
               "--scope", "tables you can delete from", "--probe", "events", "--probe", " Events ")
        check("two probes on the SAME member is refused (a repeat proves nothing)",
              r.returncode != 0 and "name the same member" in r.stderr)
        r = gl(proj, "claim", "--assert", "x", "--read", real, "--probe", "events",
               "--probe", "orders")
        check("probes with no category named are refused",
              r.returncode != 0 and "is not a boundary" in r.stderr)
        r = gl(proj, "claim", "--assert", "only the events table refuses deletes", "--read", real,
               "--scope", "tables you can delete from", "--probe", "events", "--probe", "orders")
        check("two DIFFERENT probes is accepted",
              r.returncode == 0 and "CLAIM sourced" in r.stdout)
        check("both probes are handed back, not just the count",
              "events" in r.stdout and "orders" in r.stdout)
        check("an admitted scope claim says what it cannot check (INV6)",
              "CANNOT CHECK" in r.stdout)
        check("a claim filed WITH a scope is not also nudged about one",
              r.returncode == 0 and "reads category-shaped" not in r.stdout)
        # #42: the control for the line above, in the same observation. "The nudge stood down because
        # a scope was filed" and "the detector never fires" print identically, so the IDENTICAL
        # sentence goes back in without --scope and has to come out flagged.
        bare = gl(proj, "claim", "--assert", "only the events table refuses deletes", "--read", real)
        check("...and the identical sentence with NO scope IS nudged — the silence was the scope",
              "reads category-shaped" in bare.stdout and "reads category-shaped" not in r.stdout)
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            log = f.read()
        check("both probes are recorded, in the log a later session inherits",
              '"probes": ["events", "orders"]' in log)
        check("the category they belong to is recorded with them",
              '"scope": "tables you can delete from"' in log)
        # The detector is a NUDGE, never a gate: enforcement that depends on reading English is not
        # enforcement (INV1), and a guard that blocked on a false positive would block its own fix
        # (INV5). So a set-shaped assertion filed as an instance is admitted — and made loud.
        flagged = gl(proj, "claim", "--assert", "deletes are restricted on the events table",
                     "--read", real)
        check("a category-shaped assertion filed as an instance is FLAGGED, not blocked",
              flagged.returncode == 0 and "reads category-shaped" in flagged.stdout)
        check("the nudge names the workaround as the tell",
              "WORKAROUND" in flagged.stdout)
        r = gl(proj, "claim", "--assert", "the retry helper sleeps 2s between attempts",
               "--read", real)
        check("an ordinary instance claim is untouched — no nudge, nothing owed",
              r.returncode == 0 and "CLAIM sourced" in r.stdout
              and "reads category-shaped" not in r.stdout)
        # #42 again: a detector that never fires also leaves an ordinary claim alone. The pair is
        # the evidence — one sentence through, one sentence flagged, one detector, two verdicts.
        check("...and the detector that let it through had just fired on a set-shaped sibling",
              "reads category-shaped" in flagged.stdout
              and "reads category-shaped" not in r.stdout)
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            log = f.read()
        check("an instance claim records an empty scope, so set claims stay greppable apart",
              '"scope": null, "probes": []' in log)
        # #14 #11 #13: an instrument is A TEST WHOSE SUBJECT IS REALITY — and this project already
        # holds that a test which cannot fail certifies the defect instead of catching it. These are
        # that idea one layer out. Three ways the same number lies, one admission gate: a reading is
        # a DELTA scoped to the interaction and never a lifetime total (#14, the structural one — two
        # endpoints is what makes the other two enforceable rather than advisory); a metric earns
        # evidence status only with a null AND a positive control (#11); an optimized proxy must
        # declare the user-visible harm it stands for, so the connection is RE-CHECKED when the
        # number moves instead of assumed to have survived (#13). Every rejection below asserts
        # game_loop's own message text, never a bare non-zero exit: argparse's "invalid choice" also
        # exits non-zero, and a test that passes for THAT reason is a test that cannot fail.
        print("instruments (a number is evidence only once it is controlled, #14 #11 #13):")
        HARM = "audible dropouts the listener actually hears"
        CONN = "each underrun empties the ring buffer, and an empty buffer plays as silence"
        reg = ["instrument", "--register", "underruns", "--measures", HARM, "--connects", CONN,
               "--null", "0,0", "--positive", "0,12"]

        def without(flag):    # the same registration minus one flag and its value
            i = reg.index(flag)
            return reg[:i] + reg[i + 2:]

        r = gl(proj, *without("--measures"))
        check("refuses an instrument that declares no user-visible harm (#13)",
              r.returncode != 0 and "GAMELOOP ✗" in r.stderr and "--measures" in r.stderr)
        r = gl(proj, *without("--connects"))
        check("refuses an instrument that never says HOW the number reaches that harm (#13)",
              r.returncode != 0 and "--connects" in r.stderr)
        r = gl(proj, *without("--null"))
        check("refuses an instrument with no null control (#11)",
              r.returncode != 0 and "no null control" in r.stderr)
        r = gl(proj, *without("--positive"))
        check("refuses an instrument with no positive control (#11)",
              r.returncode != 0 and "no positive control" in r.stderr)
        r = gl(proj, *(without("--null") + ["--null", "4053"]))
        check("refuses a control given as ONE absolute value — a control is a delta too (#14)",
              r.returncode != 0 and "TWO endpoint" in r.stderr)
        r = gl(proj, *(without("--null") + ["--null", "0,4053"]))
        check("a NON-ZERO null control is refused, and the refusal NAMES it (#11)",
              r.returncode != 0 and "null control is NOT zero" in r.stderr and "4053" in r.stderr)
        r = gl(proj, *(without("--positive") + ["--positive", "7,7"]))
        check("refuses an instrument whose positive control never caught anything (#11)",
              r.returncode != 0 and "positive control never moved" in r.stderr)
        r = gl(proj, *reg)
        check("admits an instrument with a declared harm and both controls",
              r.returncode == 0 and "INSTRUMENT ADMITTED" in r.stdout)
        r = gl(proj, *reg)
        check("refuses to silently re-register an admitted instrument (re-controlling is logged)",
              r.returncode != 0 and "already admitted" in r.stderr)
        # #14's incident, in the refusal: 157839 of 176001 read as a 90% failure rate; deltas across
        # the interaction showed zero in thirty trials, because the rest accrued while idle.
        r = gl(proj, "measure", "--instrument", "underruns", "--before", "176001")
        check("refuses a single absolute reading — a reading is two endpoints (#14)",
              r.returncode != 0 and "TWO endpoints" in r.stderr and "157839" in r.stderr)
        r = gl(proj, "measure", "--instrument", "no-such-counter", "--before", "0", "--after", "1")
        check("refuses a reading of an instrument that was never admitted",
              r.returncode != 0 and "never admitted" in r.stderr)
        # 40 → 290 is Δ250: the delta appears NOWHERE in the arguments, so only the tool can print it.
        r = gl(proj, "measure", "--instrument", "underruns", "--before", "40", "--after", "290",
               "--notes", "thirty trials, active playback only")
        check("accepts a two-endpoint reading and computes the delta ITSELF (#14)",
              r.returncode == 0 and "Δ 250" in r.stdout)
        r = gl(proj, "claim", "--assert", "underruns dropped", "--metric", "no-such-counter")
        check("a claim citing an unregistered instrument is refused (#11)",
              r.returncode != 0 and "never admitted" in r.stderr
              and "instrument --register" in r.stderr)
        r = gl(proj, "claim", "--assert", "the fix cut underruns", "--metric", "underruns")
        check("a claim backed by an admitted instrument's reading is accepted",
              r.returncode == 0 and "Δ 250" in r.stdout)
        # 250 → 143 is the #13 incident's 43% reduction: real, correctly computed, on a counter that
        # had decoupled from the harm in exactly the regime the fix created. The tool does that
        # arithmetic; the caller is never asked for a ratio.
        gl(proj, "measure", "--instrument", "underruns", "--before", "300", "--after", "443")
        r = gl(proj, "claim", "--assert", "the fix cut underruns 43%", "--metric", "underruns")
        check("once the metric MOVES, the claim is refused until the connection is re-checked (#13)",
              r.returncode != 0 and "--recheck" in r.stderr and "42.8%" in r.stderr)
        check("the re-check refusal names the harm the proxy stands for, not just the number (#13)",
              HARM in r.stderr)
        r = gl(proj, "claim", "--assert", "the fix cut underruns 43%", "--metric", "underruns",
               "--recheck", "listened to 20 clips: no dropout reached the ear, the count still tracks")
        check("a re-checked connection admits the moved metric", r.returncode == 0)
        r = gl(proj, "status")
        check("the declared harm survives into status, so it can be re-checked later (#13)",
              HARM in r.stdout and CONN in r.stdout)
        check("status states what these controls do NOT catch — the RIGHT metric (INV6)",
              "RIGHT metric" in r.stdout)
        check("instrument --list shows the admitted instrument",
              "underruns" in gl(proj, "instrument", "--list").stdout)
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            log = f.read()
        check("the log carries both endpoints and the delta, so the reading is reproducible later",
              '"kind": "measure"' in log and '"before": 40' in log and '"after": 290' in log
              and '"delta": 250' in log)
        check("the declared harm and both controls are permanent in the log (#11 #13)",
              '"kind": "instrument"' in log and HARM in log and '"null"' in log
              and '"positive"' in log)
        check("the re-check is on the record, attached to the reading that moved (#13)",
              '"recheck"' in log and "no dropout reached the ear" in log)
        # instruments are per-session state like pins: a control is a MEASUREMENT taken in one run's
        # regime, and a control inherited by a later run is exactly the "assumed to have survived"
        # failure #13 describes. (The reading itself is in the shared log, where it is reproducible.)
        r = gl(proj, "claim", "--assert", "x", "--metric", "underruns", sid="sess-instr-b")
        check("an instrument admitted in one session is not evidence in another",
              r.returncode != 0 and "never admitted" in r.stderr)
        r = gl(proj, "instrument", "--release", "underruns")
        check("refuses to retire an instrument without --notes (retiring is a stated decision)",
              r.returncode != 0 and "GAMELOOP ✗" in r.stderr)
        r = gl(proj, "instrument", "--release", "underruns", "--notes", "corrected instrument lands")
        check("retires an instrument by name", r.returncode == 0 and "RETIRED" in r.stdout)
        r = gl(proj, "claim", "--assert", "z", "--metric", "underruns")
        check("a retired instrument no longer backs a claim",
              r.returncode != 0 and "never admitted" in r.stderr)
        # REGRESSION GUARD, and it passes in BOTH states by construction: --metric ADDS a kind of
        # evidence, it must not touch the document path that INV2 rests on.
        check("the document path is unchanged — --read alone still sources a claim",
              gl(proj, "claim", "--assert", "y", "--read", real).returncode == 0)

        # #21: the commit gate asks whether a change was VERIFIED. It never asked whether it was
        # INTENDED. A formatter run against a whole directory reformatted a dozen files the session
        # had never opened, `git add -A` swept them in, and the commit message described five other
        # ones. So the guard records what THIS session wrote through Write/Edit and, at commit, names
        # the staged excess — stated, never blocked.
        # Own sandbox: a real git repo with an EMPTY verify.yaml, so the owed-checks gate cannot deny
        # and swallow the warning under test. Every "stays quiet" check below also asserts something
        # POSITIVE (the recorded set, or a sibling case that does fire) — a bare absence would pass
        # against code that never implemented the check at all.
        print("write guard (commit blast radius):")
        br = make_sandbox()
        try:
            with open(os.path.join(br, ".game_loop", "verify.yaml"), "w") as f:
                f.write("")
            edited_f = os.path.join(br, ".game_loop", "sessions", "sess-blast", "edited.txt")

            def brgit(*args):
                return subprocess.run(["git", "-c", "user.email=t@example.invalid",
                                       "-c", "user.name=tester", "-c", "commit.gpgsign=false",
                                       *args], cwd=br, capture_output=True, text=True)

            def brwrite(rel):
                p = os.path.join(br, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(rel + "\n")

            def bredit(rel, sid="sess-blast"):
                """The REAL guard on a real Write — the only thing that records a session edit."""
                return guard(br, {"tool_name": "Write", "session_id": sid,
                                  "tool_input": {"file_path": os.path.join(br, rel)}}, sid=sid)

            def brcommit(sid="sess-blast"):
                return guard(br, {"tool_name": "Bash", "session_id": sid, "cwd": br,
                                  "tool_input": {"command": "git commit -m x"}}, sid=sid)

            brgit("init", "-q")
            brwrite("README.md")
            brgit("add", "-A")
            brgit("commit", "-q", "-m", "init")
            for rel in ("lib/parser.dart", "lib/lexer.dart"):
                brwrite(rel)
                bredit(rel)
            brgit("add", "--", "lib/parser.dart", "lib/lexer.dart")
            r = brcommit()
            recorded = open(edited_f).read().split() if os.path.exists(edited_f) else []
            check("a Write inside the repo is recorded as this session's work",
                  set(recorded) == {"lib/parser.dart", "lib/lexer.dart"})
            check("a commit staging only session-edited files stays quiet",
                  not denied(r) and "NEVER EDITED" not in r.stdout and recorded)

            # the reported incident: a directory-wide formatter, then `git add -A`
            for i in range(12):
                brwrite(f"lib/untouched_{i}.dart")
            brgit("add", "-A")
            r = brcommit()
            check("a commit staging untouched files names them, and says how many",
                  "COMMIT INCLUDES 12 FILES THIS SESSION NEVER EDITED" in r.stdout
                  and "lib/untouched_0.dart" in r.stdout and "more" in r.stdout)
            check("the blast-radius warning never blocks the commit",
                  "NEVER EDITED" in r.stdout and not denied(r) and r.returncode == 0)
            check("the warning states what its edited set cannot see (INV6)",
                  "NEVER EDITED" in r.stdout and "Bash" in r.stdout
                  and "silence here is not evidence" in r.stdout.lower())
            rb = brcommit(sid="sess-blast-other")
            check("a session with no recorded edits accuses nobody (no evidence, no noise)",
                  "NEVER EDITED" in r.stdout and "NEVER EDITED" not in rb.stdout)

            # generated and vendored output is somebody else's: exempt, or the warning cries wolf on
            # every lockfile and stops being read. One plain untouched file rides along, so this
            # asserts a live check that skipped the right paths — not a silent one.
            for i in range(12):
                os.remove(os.path.join(br, f"lib/untouched_{i}.dart"))
            brgit("reset", "-q")
            for rel in ("vendor/dep/lib.js", "node_modules/pkg/index.js", "pubspec.lock",
                        "lib/model.g.dart", "lib/swept_in.dart"):
                brwrite(rel)
            brgit("add", "-A")
            r = brcommit()
            check("generated and vendored paths do not trigger the warning",
                  "COMMIT INCLUDES 1 FILE THIS SESSION NEVER EDITED" in r.stdout
                  and "lib/swept_in.dart" in r.stdout
                  and "vendor/dep/lib.js" not in r.stdout and "pubspec.lock" not in r.stdout
                  and "model.g.dart" not in r.stdout)
            brlog_f = os.path.join(br, ".game_loop", "log.jsonl")
            brlog = open(brlog_f).read() if os.path.exists(brlog_f) else ""
            check("a widened commit is permanent in the log", '"commit_unedited"' in brlog)
        finally:
            shutil.rmtree(br, ignore_errors=True)
        # #24 / #26: the session transcript is a LIVE, adversarial file — appended to while it is
        # read, so its last line is routinely half-written, and one pasted image lands as a single
        # base64 line of megabytes. The reader must stay bounded, must fail soft per line, and must
        # SAY what it dropped. And the harness's own tool-denials must be read as a FIELD of the
        # decoded record: the string "toolDenialKind" is all over a normal transcript as data.
        print("transcript reader (bounded, fail-soft, observable — #24):")
        tr = os.path.join(proj, "live-transcript.jsonl")

        def write_transcript(*records):
            with open(tr, "w") as f:
                for rec in records:
                    f.write(rec if isinstance(rec, str) else json.dumps(rec))
                    f.write("\n")

        def assistant(text):
            return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}

        def stop_on_transcript(sid):
            return gl(proj, "stopgate",
                      stdin=json.dumps({"session_id": sid, "transcript_path": tr}))

        def log_text():
            with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
                return f.read()

        # A fresh session per case: two consecutive blocks trip the circuit breaker, and these cases
        # each expect a block.
        for sid in ("sess-tr-half", "sess-tr-bad", "sess-tr-big"):
            gl(proj, "mandate", "--set", "read the transcript honestly", sid=sid)

        write_transcript(assistant("Which branch should I use?"),
                         '{"type":"assistant","message":{"content":[{"type":"tex')
        r = stop_on_transcript("sess-tr-half")
        check("a half-written final line still yields the correct earlier record",
              r.returncode == 2 and "asking the human a question" in r.stderr)

        seen = len(log_text())
        write_transcript(assistant("Which branch should I use?"), "{ not json at all")
        r = stop_on_transcript("sess-tr-bad")
        added = log_text()[seen:]
        check("a malformed record is skipped, and the skip is observable in the log",
              r.returncode == 2 and '"kind": "transcript_skipped"' in added
              and '"skipped": 1' in added)

        seen = len(log_text())
        write_transcript(assistant("Which branch should I use?"),
                         {"type": "user", "message": {"content": [
                             {"type": "image", "source": {"data": "A" * (300 * 1024)}}]}})
        r = stop_on_transcript("sess-tr-big")
        added = log_text()[seen:]
        check("an oversized single line does not dominate the read",
              r.returncode == 2 and "asking the human a question" in r.stderr
              and '"oversized": 1' in added)
        for sid in ("sess-tr-half", "sess-tr-bad", "sess-tr-big"):
            gl(proj, "mandate", "--clear", "--notes", "done", sid=sid)

        print("harness tool-denials as enforcement evidence (#26):")
        r = gl(proj, "status", sid="sess-den-none")
        check("a session with no transcript in reach says so, and does not report zero denials",
              "no transcript in reach" in r.stdout and "absence of signal" in r.stdout)

        # The word as DATA: an assistant message about the field, and tool output quoting a grep of
        # it. A text match would call these denials; reading the field calls them what they are.
        write_transcript(assistant("The harness records a refusal as toolDenialKind in the "
                                   "transcript — read in docs/how-it-works.md."),
                         {"type": "user", "message": {"content": [
                             {"type": "tool_result",
                              "content": "grep -rn toolDenialKind . -> 15 matches"}]}})
        stop_on_transcript("sess-den-text")
        r = gl(proj, "status", sid="sess-den-text")
        check("a record whose TEXT contains toolDenialKind is NOT counted as a denial",
              "denials: 0" in r.stdout)
        # Scoped to the denials line itself, not the whole readout: the COVERAGE block legitimately
        # says a rail "is blind" about a DIFFERENT rail, and grepping all of stdout for the word made
        # this check fail the moment that block landed — a false alarm about behaviour that is right.
        den_line = next((l for l in r.stdout.splitlines() if l.startswith("denials:")), "")
        check("an empty result reads as armed, nothing tripped it — not a blind spot",
              "armed, nothing tripped it" in den_line and "blind" not in den_line)

        # The same word, this time as a FIELD nested three levels down in the record.
        write_transcript(assistant("Writing the file now."),
                         {"type": "user", "message": {"content": [
                             {"type": "tool_result", "toolDenialKind": "deny_rule",
                              "content": "the write was refused"}]}})
        stop_on_transcript("sess-den-field")
        r = gl(proj, "status", sid="sess-den-field")
        check("a record carrying the FIELD is counted as a denial",
              "the harness refused 1 tool call" in r.stdout and "deny_rule ×1" in r.stdout)
        check("the denial readout names how it was read — field, not grep",
              "read as a field in the decoded record, never grepped" in r.stdout)
        # #23: the write guard reads Bash. An MCP server can perform the SAME irreversible act with
        # no shell command at all — a `DELETE FROM` through a database tool, a send through a mail
        # tool, a force-op through a git-host tool — and guard-writes-impl.sh has said in prose since
        # it was written that it "DOES NOT: catch mutations made via MCP tools". A gap stated in
        # prose is a gap that gets walked through (INV1). guard-mcp.sh is that prose made executable:
        # matched on `mcp__.*`, it classifies BEFORE the call runs and fails CLOSED on ambiguity.
        print("MCP guard (#23):")

        def mcpguard(payload, sid=None):
            return subprocess.run([os.path.join(proj, ".game_loop", "bin", "guard-mcp.sh")],
                                  input=json.dumps(payload), capture_output=True, text=True,
                                  env=_env(proj, sid))

        def why(res):
            """The deny reason as text — assertions are on the MESSAGE, not merely on non-zero."""
            try:
                return json.loads(res.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            except (ValueError, KeyError, TypeError):
                return ""

        # The must-ALLOW / must-DENY fixture pair the issue asks for: the SAME read-named tool on the
        # SAME server, separated only by the argument it carries.
        ro = {"tool_name": "mcp__db__query", "tool_input": {"sql": "SELECT id FROM users LIMIT 5"}}
        check("a read-only MCP call passes (SELECT through a query tool)",
              not denied(mcpguard(ro)))
        rw = {"tool_name": "mcp__db__query",
              "tool_input": {"sql": "DELETE FROM users WHERE id = 1"}}
        r = mcpguard(rw)
        check("the same tool carrying a destructive SQL argument is blocked",
              denied(r) and "classified as MUTATING" in why(r)
              and "a mutating SQL statement in argument /sql" in why(r))
        # The name alone is enough when the verb is unmistakable — no argument needed.
        r = mcpguard({"tool_name": "mcp__mail__deleteMessage", "tool_input": {"id": "abc"}})
        check("a delete verb in the tool name is blocked",
              denied(r) and "irreversible verb: 'delete'" in why(r))
        r = mcpguard({"tool_name": "mcp__chat__sendMessage",
                      "tool_input": {"channel": "#general", "text": "hi"}})
        check("a send verb in the tool name is blocked (a send is not undoable)",
              denied(r) and "NAME's verb mutates: 'send'" in why(r))
        r = mcpguard({"tool_name": "mcp__git__syncBranch", "tool_input": {"force": True}})
        check("a truthy force flag is blocked even under a mild verb",
              denied(r) and "a destructive flag set true: /force" in why(r))
        # Nested arguments: a mutation three levels down is still a mutation.
        r = mcpguard({"tool_name": "mcp__api__call",
                      "tool_input": {"request": {"opts": {"method": "DELETE"}}}})
        check("a mutating request method nested in the arguments is blocked",
              denied(r) and "/request/opts/method" in why(r))
        # THE fail-closed case, and the opposite default from the Bash write guard.
        r = mcpguard({"tool_name": "mcp__vendor__frobnicate", "tool_input": {"x": 1}})
        check("an unrecognised verb fails CLOSED (ambiguous is refused, not allowed)",
              denied(r) and "could not be classified" in why(r)
              and "FAILS CLOSED" in why(r))
        # Real servers often repeat their own name in the tool name. If that prefix is not stripped,
        # the VERB never reaches the verb slot and every tool on such a server reads as
        # unclassifiable — fail-closed degrades into fail-useless, which is how a guard gets removed.
        r = mcpguard({"tool_name": "mcp__pebble__pebble_run_command", "tool_input": {"cmd": "x"}})
        check("a server that repeats its own name still lands the verb (deny side)",
              denied(r) and "NAME's verb mutates: 'run'" in why(r))
        check("a server that repeats its own name still lands the verb (allow side)",
              not denied(mcpguard({"tool_name": "mcp__db__db_list_tables", "tool_input": {}})))
        # Scoped to MCP: a repo with no MCP servers must behave exactly as before.
        check("a non-MCP tool is not this guard's business (passes through untouched)",
              not denied(mcpguard({"tool_name": "Bash",
                                   "tool_input": {"command": "rm -rf ~/outside"}})))

        print("MCP guard (fail-closed shim, and INV5):")
        mcp_impl_f = os.path.join(proj, ".game_loop", "bin", "guard-mcp-impl.sh")
        with open(mcp_impl_f) as f:
            mcp_impl_src = f.read()
        with open(mcp_impl_f, "w") as f:
            f.write("this is ( not valid bash\n")
        r = mcpguard(ro)
        check("a malformed MCP impl DENIES (fails closed, unlike the write guard)",
              denied(r) and "the MCP guard cannot run" in why(r))
        # WHY failing closed here is safe where it would be fatal in guard-writes: this hook is
        # matched on `mcp__.*` only, so a broken MCP guard never blocks the Write/Edit/Bash call that
        # would REPAIR it. INV5 holds because of the SCOPE, not because of the default.
        check("a malformed MCP impl still leaves the write path open (INV5: its own fix)",
              not denied(guard(proj, {"tool_name": "Write",
                                      "tool_input": {"file_path": os.path.join(proj, "fix.sh")}})))
        with open(mcp_impl_f, "w") as f:                 # restore before the authorize checks
            f.write(mcp_impl_src)

        print("MCP guard (authorize → consume):")
        gl(proj, "authorize", "--path", "mcp__mail__deleteMessage", "--reason", "user said ok")
        pm = {"tool_name": "mcp__mail__deleteMessage", "tool_input": {"id": "abc"}}
        check("an authorized MCP tool is allowed once", not denied(mcpguard(pm)))
        check("the MCP authorization is single-use (spent → denied)", denied(mcpguard(pm)))
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            log = f.read()
        check("an MCP spend is logged as authorized_mcp naming the tool",
              '"authorized_mcp"' in log and '"tool": "mcp__mail__deleteMessage"' in log)
        # The two escape hatches must not be interchangeable in EITHER direction, or one authorized
        # act would quietly buy a different one.
        gl(proj, "authorize", "--path", os.path.expanduser("~/mcp-crosstalk"),
           "--reason", "user said ok")
        check("a filesystem authorization cannot be spent by an MCP call",
              denied(mcpguard({"tool_name": "mcp__mail__deleteMessage",
                               "tool_input": {"id": "z"}})))
        gl(proj, "authorize", "--path", "mcp__db__dropTable", "--reason", "user said ok")
        check("an MCP authorization cannot be spent by a filesystem write",
              denied(guard(proj, {"tool_name": "Write",
                                  "tool_input": {"file_path": os.path.expanduser("~/xtalk.txt")}})))

        print("MCP guard (teaching it, without opening a bypass):")
        c = json.load(open(cf)); c["mcp_read_only_tools"] = ["mcp__vendor__"]
        json.dump(c, open(cf, "w"))
        check("a configured read-only server resolves the ambiguous case",
              not denied(mcpguard({"tool_name": "mcp__vendor__frobnicate",
                                   "tool_input": {"x": 1}})))
        r = mcpguard({"tool_name": "mcp__vendor__frobnicate",
                      "tool_input": {"sql": "DROP TABLE users"}})
        check("the read-only list can never silence a mutating ARGUMENT",
              denied(r) and "classified as MUTATING" in why(r))
        r = mcpguard({"tool_name": "mcp__vendor__deleteEverything", "tool_input": {}})
        check("the read-only list can never silence a mutating VERB",
              denied(r) and "irreversible verb: 'delete'" in why(r))

        # #19: a guard that exists but is never wired in is not a guard. Assert the registration
        # itself — in THIS repo and in the template every install merges — and that install.sh
        # actually ships the files, and that the new bin file owes a check (#25).
        print("MCP guard (wired in, not just written):")
        for label, path in (("this repo's .claude/settings.json",
                             os.path.join(REPO, ".claude", "settings.json")),
                            ("the shipped templates/settings.hooks.json",
                             os.path.join(REPO, "templates", "settings.hooks.json"))):
            with open(path) as f:
                entries = json.load(f)["hooks"]["PreToolUse"]
            wired = [e for e in entries if e.get("matcher") == "mcp__.*"
                     and any("guard-mcp.sh" in h.get("command", "") for h in e.get("hooks", []))]
            check(f"the mcp__.* hook is registered in {label}", len(wired) == 1)
        with open(os.path.join(REPO, "install.sh")) as f:
            inst = f.read()
        check("install.sh copies AND chmods both MCP guard files",
              inst.count("guard-mcp.sh") >= 2 and inst.count("guard-mcp-impl.sh") >= 2)
        with open(os.path.join(SRC_GAME_LOOP, "verify.yaml")) as f:
            vy = f.read()
        check("both MCP guard files owe the test suite in verify.yaml (#25)",
              '".game_loop/bin/guard-mcp.sh":' in vy
              and '".game_loop/bin/guard-mcp-impl.sh":' in vy)

        # #27: the gates above all check that WORK HAPPENED. None of them checks that a FIX WORKS.
        # A bug was diagnosed exhaustively and the fix shipped as a PR whose generated code did not
        # compile — under three green signals, every one answering a question nobody asked. So a fix
        # is owed a proof of its OWN OUTPUT, kept distinct from the diagnosis's repro, and a handback
        # that reports a fix without one is warned (never blocked — #20's posture, and INV5).
        # Every rejection below asserts the game_loop die() text, not merely a non-zero exit: an
        # unimplemented subcommand also exits non-zero, and a test that passes for that reason is a
        # test that cannot fail.
        print("fix proofs (a verified diagnosis is not a verified fix, #27):")
        fxd = os.path.join(proj, "fixwork")
        os.makedirs(fxd, exist_ok=True)

        def fxfile(name, body):
            p = os.path.join(fxd, name)
            with open(p, "w") as f:
                f.write(body)
            return p

        # the fix's OWN output (generated code), the repro that proved the bug, and the REAL
        # consumer's verdict from either side of the change.
        produces = fxfile("generated_model.dart", "class Model { Model(); }\n")
        repro = fxfile("repro.txt", "reproduced: Model() throws on a null id\n")
        repro_copy = fxfile("repro_copy.txt", "reproduced: Model() throws on a null id\n")
        vbefore = fxfile("compile_before.txt", "generated_model.dart:1:14: error: expected ';'\n")
        vbefore_again = fxfile("compile_before_again.txt",
                               "generated_model.dart:1:14: error: expected ';'\n")
        vafter = fxfile("compile_after.txt",
                        "generated_model.dart: Built build/app.js — 0 errors\n")

        base = ["fix", "--prove", "null-id", "--promises",
                "the generated model compiles and accepts a null id"]
        r = gl(proj, *base, "--produces", produces, "--before", vbefore, "--observed", vafter)
        check("a fix proof that never names the diagnosis's repro is refused",
              r.returncode != 0 and "GAMELOOP ✗" in r.stderr and "--diagnosis" in r.stderr)
        r = gl(proj, *base, "--diagnosis", repro, "--before", vbefore, "--observed", vafter)
        check("a fix proof that never names the fix's own output is refused",
              r.returncode != 0 and "GAMELOOP ✗" in r.stderr and "--produces" in r.stderr)
        r = gl(proj, *base, "--produces", produces, "--diagnosis", repro,
               "--before", "/no/such/verdict.txt", "--observed", vafter)
        check("a fix proof whose before verdict does not resolve is refused",
              r.returncode != 0 and "--before does not resolve" in r.stderr)
        r = gl(proj, *base, "--produces", "/no/such/generated.dart", "--diagnosis", repro,
               "--before", vbefore, "--observed", vafter)
        check("a fix proof whose produced output does not resolve is refused",
              r.returncode != 0 and "--produces does not resolve" in r.stderr)
        # naming the repro as the thing the fix emits is the collapse, one flag early.
        r = gl(proj, *base, "--produces", repro, "--diagnosis", repro,
               "--before", vbefore, "--observed", vafter)
        check("naming the repro as the fix's own output is refused",
              r.returncode != 0 and "--produces and --diagnosis are the same file" in r.stderr)
        # THE refusal. "the diagnosis's reproduction still reproduced — it was never a test of the
        # fix." If one artifact can satisfy both claims, the gate is already defeated.
        r = gl(proj, *base, "--produces", produces, "--diagnosis", repro,
               "--before", vbefore, "--observed", repro)
        check("a proof that is merely the diagnosis repro is refused",
              r.returncode != 0 and "the observed verdict IS the diagnosis's repro" in r.stderr
              and "COMING BACK GOOD" in r.stderr)
        r = gl(proj, *base, "--produces", produces, "--diagnosis", repro,
               "--before", vbefore, "--observed", repro_copy)
        check("a byte-identical COPY of the repro is refused too (a rename is not a proof)",
              r.returncode != 0 and "byte-identical" in r.stderr)
        # the consumer re-run after the "fix" and saying exactly what it said before: the incident.
        r = gl(proj, *base, "--produces", produces, "--diagnosis", repro,
               "--before", vbefore, "--observed", vbefore_again)
        check("an unmoved verdict is refused — the generated code still does not compile",
              r.returncode != 0 and "IDENTICAL" in r.stderr and "STILL REPRODUCES" in r.stderr)
        r = gl(proj, *base, "--produces", produces, "--diagnosis", repro, "--before", vbefore,
               "--observed", vafter, "--expect", "0 warnings")
        check("--expect absent from the observed verdict is refused (it moved, but not to the promise)",
              r.returncode != 0 and "does not appear in the observed verdict" in r.stderr)
        r = gl(proj, *base, "--produces", produces, "--diagnosis", repro, "--before", vbefore,
               "--observed", vafter, "--expect", "generated_model.dart")
        check("--expect that was ALREADY true before the fix is refused",
              r.returncode != 0 and "ALREADY in the before verdict" in r.stderr)
        # the handback warning, before anything is proved.
        # Each of the three checks below is a DIFFERENTIAL against `warned`, not a bare absence: a
        # harness that never printed the warning at all would satisfy "stays quiet" and "is
        # silenced" for free, and a check that passes when the feature is missing is not a check.
        warned = gl(proj, "checkpoint", "--notes", "fixed the null-id crash in the generator")
        check("a fix reported at a handback with no proof of the fixed artifact is warned about",
              warned.returncode == 0 and "FIX CLAIMED, NOT PROVED" in warned.stdout
              and "verified diagnosis is not a verified fix" in warned.stdout)
        check("the fix warning never blocks the checkpoint",
              warned.returncode == 0 and "FIX CLAIMED, NOT PROVED" in warned.stdout
              and "✓ CHECKPOINT" in warned.stdout)
        check("the fix warning says what it does NOT catch (INV6)",
              "any rephrasing walks straight past it" in warned.stdout
              and "not evidence the fix holds" in warned.stdout)
        r = gl(proj, "checkpoint", "--notes", "still tracing the generator's null handling")
        check("notes that report no fix stay quiet (no evidence, no noise)",
              r.returncode == 0 and "FIX CLAIMED" in warned.stdout
              and "FIX CLAIMED" not in r.stdout)
        # a real before/after on the fix's own output, and the promise named in --expect.
        r = gl(proj, *base, "--produces", produces, "--diagnosis", repro, "--before", vbefore,
               "--observed", vafter, "--expect", "0 errors")
        check("a real before/after on the fixed artifact's own output is accepted",
              r.returncode == 0 and "FIX PROVED" in r.stdout
              and "moved to what the fix promised" in r.stdout)
        check("an accepted fix proof states what it does NOT catch (INV6)",
              "DOES NOT CATCH" in r.stdout and "really regenerated" in r.stdout
              and "not to the right fix" in r.stdout)
        r = gl(proj, "checkpoint", "--notes", "fixed the null-id crash in the generator")
        check("a proved fix silences the handback warning (same notes that warned a moment ago)",
              r.returncode == 0 and "✓ CHECKPOINT" in r.stdout
              and "FIX CLAIMED" in warned.stdout and "FIX CLAIMED" not in r.stdout)
        r = gl(proj, "status")
        check("status carries the fix proof and its promised outcome through compaction",
              "FIXES" in r.stdout and "accepts a null id" in r.stdout
              and "the repro, kept separate on purpose" in r.stdout)
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            fxlog = f.read()
        check("the proof is greppable in the log as compared artifacts, not a verdict",
              '"kind": "fix_proof"' in fxlog and '"before_digest"' in fxlog
              and '"observed_digest"' in fxlog and '"diagnosis_digest"' in fxlog)
        check("a fix reported without proof is permanent in the log (INV4 wants the entry)",
              '"kind": "fix_unproved"' in fxlog)
        # a fix proof is a fact about THIS tree at ONE moment — it cannot quiet a sibling's handback.
        r = gl(proj, "checkpoint", "--notes", "fixed the null-id crash", sid="sess-fix-b")
        check("a fix proof does not leak into another session's handback",
              r.returncode == 0 and "FIX CLAIMED, NOT PROVED" in r.stdout)
        r = gl(proj, "fix", "--release", "null-id")
        check("refuses to retire a fix proof without --notes (a handback went quiet on it)",
              r.returncode != 0 and "--notes" in r.stderr)
        r = gl(proj, "fix", "--release", "null-id", "--notes", "the generator was rewritten since")
        check("releases a fix proof by name",
              r.returncode == 0 and "FIX PROOF RELEASED" in r.stdout)
        r = gl(proj, "checkpoint", "--notes", "fixed the null-id crash in the generator")
        check("after a release, the handback warns about that fix again",
              r.returncode == 0 and "FIX CLAIMED, NOT PROVED" in r.stdout)
        gl(proj, "mandate", "--set", "land the generator fix")
        r = gl(proj, "mandate", "--clear", "--notes", "shipped — the null-id bug is fixed")
        check("mandate --clear warns about an unproved fix too (the loudest 'shipped' there is)",
              r.returncode == 0 and "FIX CLAIMED, NOT PROVED" in r.stdout
              and "✓ MANDATE released" in r.stdout)

        # #12: A SUM IS NOT A DISTRIBUTION. An aggregate hides its own shape, and a run optimizing
        # against one reads structure into a single outlier — this happened three times in ONE
        # session off the same event before anyone caught it. The fixture below IS that incident:
        # 1066.7 units of damage against 0.0 looked like a total elimination, and one event of
        # thirty carried 96% of it. Its own session, so nothing above leaks into it.
        # THE ARITHMETIC IS THE POINT. The endpoints are 100 → 1166.7 and the events are pasted
        # verbatim, so the total (1066.7), the dominating share (96.0%) and the per-event remainder
        # (1.5) appear NOWHERE in any argument — the same trick as `Δ 250` above. If the tool did
        # not compute them, these checks cannot pass. Every rejection asserts game_loop's own message
        # text, never a bare non-zero exit: argparse's "unrecognized arguments" also exits non-zero.
        print("distributions (a sum is not a distribution, #12):")
        SD = "sess-dist"
        DHARM = "damage the player actually takes"
        DCONN = "each unit is a hitpoint off the health bar the player is watching"
        EVENTS = ",".join(["1024.0"] + ["0"] * 28 + ["42.7"])   # the issue's thirty events
        REASON = "first event after a known state transition; artifact already ruled out this session"
        gl(proj, "instrument", "--register", "damage", "--measures", DHARM, "--connects", DCONN,
           "--null", "0,0", "--positive", "0,12", sid=SD)
        r = gl(proj, "measure", "--instrument", "damage", "--before", "0", "--after", "5",
               "--events", "5", sid=SD)
        check("refuses a one-value distribution — that is the total wearing a different hat",
              r.returncode != 0 and "at least two of them" in r.stderr)
        r = gl(proj, "measure", "--instrument", "damage", "--before", "0", "--after", "5",
               "--events", "1, two, 3", sid=SD)
        check("garbage in a distribution is a REFUSAL naming the bad event, never a traceback",
              r.returncode != 0 and "event 2 of --events is not a finite number" in r.stderr
              and "Traceback" not in r.stderr)
        r = gl(proj, "measure", "--instrument", "damage", "--before", "0", "--after", "5",
               "--events", "nan,inf,1", sid=SD)
        check("nan and inf are refused too — they parse as floats and poison every comparison",
              r.returncode != 0 and "not a finite number" in r.stderr)
        # A reading with no --events at all: the shape is optional at `measure`, because measure
        # RECORDS what was read. The refusal belongs to `claim`, where an effect gets stated.
        # DECLARED: this one passes in BOTH states by construction — it is a regression guard on the
        # unchanged path, not evidence of the new gate, and it is here because the fixture needs a
        # shapeless reading anyway.
        r = gl(proj, "measure", "--instrument", "damage", "--before", "100", "--after", "1166.7",
               sid=SD)
        check("a reading with no distribution is still recorded — measure records, claim refuses",
              r.returncode == 0 and "Δ 1066.7" in r.stdout and "shape" not in r.stdout)
        r = gl(proj, "claim", "--assert", "arm B eliminated the damage", "--metric", "damage",
               "--aggregate", "sum", sid=SD)
        check("a claim derived from a SUM with no distribution supplied is refused (#12)",
              r.returncode != 0 and "no per-event breakdown" in r.stderr
              and "A SUM IS NOT A DISTRIBUTION" in r.stderr)
        r = gl(proj, "claim", "--assert", "x", "--metric", "damage", "--aggregate", "median",
               sid=SD)
        check("--aggregate takes sum · mean · pct and names the set when it does not",
              r.returncode != 0 and "--aggregate must be one of: sum · mean · pct" in r.stderr)
        # Now the incident's own distribution, attached to the reading it decomposes.
        r = gl(proj, "measure", "--instrument", "damage", "--before", "100", "--after", "1166.7",
               "--events", EVENTS, "--notes", "thirty events, arm A", sid=SD)
        check("measure PRINTS the shape and the share it computed — the step nobody took (#12)",
              r.returncode == 0 and "event 1 carries 96.0% of 1066.7" in r.stdout
              and "ONE EVENT CARRIES 96.0% OF THIS TOTAL" in r.stdout)
        check("measure states the corrected reading the incident never printed: 1.5 per event",
              "42.7 across 29 events (1.5 per event, 1 non-zero)" in r.stdout)
        r = gl(proj, "claim", "--assert", "arm B eliminated the damage entirely", "--metric",
               "damage", "--aggregate", "sum", sid=SD)
        check("one event over half the total REFUSES the claim (#12)",
              r.returncode != 0 and "ONE EVENT CARRIES THE AGGREGATE" in r.stderr)
        check("the refusal NAMES the dominating event and the share the tool computed (#12)",
              "event 1 read 1024 — 96.0% of the total" in r.stderr
              and "you were never asked for that share" in r.stderr)
        check("the refusal states what the effect looks like WITHOUT that event",
              "42.7 across 29 events — 1.5 per event, 1 non-zero" in r.stderr)
        check("the dominance refusal fires with no --aggregate too: a supplied shape is checked",
              gl(proj, "claim", "--assert", "arm B was clean", "--metric", "damage",
                 sid=SD).returncode != 0)
        r = gl(proj, "claim", "--assert", "x", "--metric", "damage", "--exclude", "1", sid=SD)
        check("excluding the outlier without a stated reason is refused (#12)",
              r.returncode != 0 and "--exclude 1 needs --because" in r.stderr
              and "An unrecorded exclusion is rediscovered" in r.stderr)
        r = gl(proj, "claim", "--assert", "x", "--metric", "damage", "--exclude", "7",
               "--because", REASON, sid=SD)
        check("excluding a DIFFERENT event still refuses, and says which one you dropped",
              r.returncode != 0 and "you dropped: event 7" in r.stderr
              and "event 1 read 1024" in r.stderr)
        r = gl(proj, "claim", "--assert", "x", "--metric", "damage", "--exclude", "31",
               "--because", REASON, sid=SD)
        check("an out-of-range --exclude is refused against the reading's own event count",
              r.returncode != 0 and "not one of this reading's 30 events" in r.stderr)
        r = gl(proj, "claim", "--assert", "x", "--metric", "damage", "--exclude", "one", sid=SD)
        check("a non-numeric --exclude refuses rather than throwing",
              r.returncode != 0 and "takes the event NUMBER" in r.stderr
              and "Traceback" not in r.stderr)
        r = gl(proj, "claim", "--assert", "no damage effect at this sample size", "--metric",
               "damage", "--aggregate", "sum", "--exclude", "1", "--because", REASON, sid=SD)
        check("the SAME distribution with a stated exclusion reason is accepted (#12)",
              r.returncode == 0 and "CLAIM sourced" in r.stdout
              and f"excluded  : event 1 (1024) — {REASON}" in r.stdout)
        check("the accepted claim prints the shape that remains — 1.5 per event, not 1066.7",
              "without it: 42.7 across 29 events — 1.5 per event, 1 non-zero" in r.stdout)
        check("the shape check states what it does NOT catch (INV6)",
              "whether what remains is an effect at all" in r.stdout)
        # A flat distribution owes nothing: no refusal, no exclusion, no ceremony — and the positive
        # assertion (the computed 27.5%) is what keeps this from passing against a check that never ran.
        gl(proj, "instrument", "--register", "stalls", "--measures", "freezes the player feels",
           "--connects", "a stall is a frame the player waits on", "--null", "0,0",
           "--positive", "0,4", sid=SD)
        gl(proj, "measure", "--instrument", "stalls", "--before", "0", "--after", "40",
           "--events", "9,10,11,10", sid=SD)
        r = gl(proj, "claim", "--assert", "the fix removed the stalls", "--metric", "stalls",
               "--aggregate", "sum", sid=SD)
        check("an evenly spread distribution passes untouched, with the top share still computed",
              r.returncode == 0 and "event 3 carries 27.5%" in r.stdout
              and "ONE EVENT CARRIES" not in r.stdout)
        r = gl(proj, "status", sid=SD)
        check("the shape and the recorded exclusion survive into status, past compaction (#12)",
              "event 1 carries 96.0%" in r.stdout and f"excluded  : event 1 (1024) — {REASON}"
              in r.stdout)
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            dlog = f.read()
        check("the per-event values and the computed share are permanent in the log",
              '"dominance"' in dlog and '"share_pct": 96.0' in dlog and '"top_event": 1' in dlog)
        check("the exclusion AND its reason are on the record — that is what stops a fourth "
              "rediscovery (#12)",
              '"excluded": {"event": 1' in dlog and REASON in dlog)
        # An all-zero distribution has no share to dominate, and dividing into one is how a gate
        # produces a traceback instead of a refusal.
        gl(proj, "instrument", "--register", "crashes", "--measures", "runs the player loses",
           "--connects", "a crash ends the run", "--null", "0,0", "--positive", "0,3", sid=SD)
        gl(proj, "measure", "--instrument", "crashes", "--before", "0", "--after", "0",
           "--events", "0,0,0,0", sid=SD)
        r = gl(proj, "claim", "--assert", "four clean runs", "--metric", "crashes",
               "--aggregate", "sum", sid=SD)
        check("an all-zero distribution divides into nothing and is admitted, not crashed",
              r.returncode == 0 and "4 events, every one of them zero" in r.stdout)
        # Wording only, exactly like the scope nudge: loud, never blocking, and it says so itself.
        # Both checks run against a SHAPELESS reading — a reading that already carries its events
        # suppresses the nudge on its own, and running the negative case against one of those would
        # be a check that passes for a reason unrelated to the wording it claims to be testing.
        # DECLARED: the negative case cannot go red against pre-change code — a nudge that does not
        # exist yet is trivially absent. It earns its place in the GREEN state, where it is the only
        # thing standing between the tell and an ordinary observation, and it asserts the claim was
        # accepted rather than merely silent.
        # #42 SINCE: that declaration explained why the negative case cannot go RED, and left it
        # resting on nothing in the green state either — neutering aggregate_tell to `return None`
        # killed exactly one assertion in the whole suite. So the negative case now carries a control
        # from the SAME instrument one call earlier, and the nudge's body is asserted rather than
        # merely its first line: an advisory whose text is unchecked can rot into a bare label.
        gl(proj, "instrument", "--register", "latency", "--measures", "waits the player notices",
           "--connects", "a long frame is a visible hitch", "--null", "0,0", "--positive", "0,8",
           sid=SD)
        gl(proj, "measure", "--instrument", "latency", "--before", "10", "--after", "40", sid=SD)
        loud_agg = gl(proj, "claim", "--assert", "latency fell 30% in total", "--metric", "latency",
                      sid=SD)
        check("an aggregate-shaped sentence with no shape behind it is made LOUD, never refused",
              loud_agg.returncode == 0 and 'reads aggregate-shaped ("30%")' in loud_agg.stdout
              and "Wording only" in loud_agg.stdout)
        check("the nudge carries the incident, not just the rule — the 96% one event hid in a total",
              "A SUM IS NOT A DISTRIBUTION" in loud_agg.stdout
              and "96% of a total" in loud_agg.stdout)
        check("the nudge hands back the exact command that fixes it, with THIS instrument named",
              "--instrument latency --before <n> --after <n>" in loud_agg.stdout
              and "--metric latency --aggregate sum" in loud_agg.stdout)
        check("the aggregate nudge says what it does NOT catch, in the incident's own words (INV6)",
              "walked straight past it" in loud_agg.stdout
              and "A single-quantity reading owes nothing here" in loud_agg.stdout)
        r = gl(proj, "claim", "--assert", "the counter read 40 on this build", "--metric",
               "latency", sid=SD)
        check("a plain observation claim gets no aggregate nudge — the tell stays quiet",
              r.returncode == 0 and "CLAIM sourced" in r.stdout
              and "reads aggregate-shaped" not in r.stdout)
        check("...against a sibling on the same instrument that DID fire one call earlier",
              "reads aggregate-shaped" in loud_agg.stdout
              and "reads aggregate-shaped" not in r.stdout)
        # The third arm, and the one the comment above only asserted in prose: a reading that already
        # CARRIES its events suppresses the nudge on its own, whatever the wording. Same tell ("30%"),
        # same claim shape, shaped reading — and the control is loud_agg, which is that sentence
        # against a shapeless one. Silence here is the shape doing its job, not the detector's.
        shaped = gl(proj, "claim", "--assert", "crashes fell 30% in total", "--metric", "crashes",
                    "--aggregate", "sum", sid=SD)
        check("the same tell over a reading that CARRIES its events is not nudged — the shape "
              "answers it",
              shaped.returncode == 0 and "reads aggregate-shaped" not in shaped.stdout
              and "reads aggregate-shaped" in loud_agg.stdout)
        # REGRESSION GUARDS, passing in BOTH states by construction: the shape check ADDS a rung to
        # the metric path; it must not touch the document keystone INV2 rests on, or the reading
        # shape #14 built.
        check("the document path is untouched — --read alone still sources a claim",
              gl(proj, "claim", "--assert", "y", "--read", real, sid=SD).returncode == 0)
        r = gl(proj, "measure", "--instrument", "crashes", "--before", "3", sid=SD)
        check("a single absolute reading is still refused — #14's gate is intact",
              r.returncode != 0 and "TWO endpoints" in r.stderr)

        # #25: a manifest of listed paths defaults to OWES-NOTHING, so a file matching no glob owes
        # nothing and passes `verify --check` in silence — a whole package built, hand-tested and
        # committed while the gate reported clean. Green meant "nothing LISTED here is stale", never
        # "nothing is unverified". Coverage is therefore computed the other way round: every changed
        # path is UNCHECKED until a rule claims it or the manifest excludes it out loud.
        # DEFAULT-DENY FOR VISIBILITY, DEFAULT-ALLOW FOR BLOCKING: the manifest ships EMPTY, so
        # "unlisted ⇒ refused" would refuse a fresh install's first commit with the fix sitting
        # behind the gate (INV5). Both halves are asserted below.
        import re as _re
        import time as _time
        print("coverage — what the checks manifest is NOT looking at (#25):")
        cv = make_sandbox()
        try:
            RULES = ('"src/**":\n  - "true"\n'
                     '"unchecked-ok":\n  - ".game_loop/**"\n  - "docs/**"\n')

            def cvwrite(rel, body="x\n"):
                p = os.path.join(cv, rel)
                if os.path.dirname(p):
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(body)

            def cvyaml(text):
                with open(os.path.join(cv, ".game_loop", "verify.yaml"), "w") as f:
                    f.write(text)

            def cvgit(*args):
                return subprocess.run(["git", "-c", "user.email=t@example.invalid",
                                       "-c", "user.name=tester", "-c", "commit.gpgsign=false",
                                       *args], cwd=cv, capture_output=True, text=True)

            def vfy(*args):
                return subprocess.run([os.path.join(cv, ".game_loop", "bin", "verify"), *args],
                                      cwd=cv, capture_output=True, text=True, env=_env())

            def cvcommit(sid="sess-cov"):
                return guard(cv, {"tool_name": "Bash", "session_id": sid, "cwd": cv,
                                  "tool_input": {"command": "git commit -m x"}}, sid=sid)

            cvgit("init", "-q")
            cvyaml(RULES)
            cvgit("add", "-A")
            cvgit("commit", "-q", "-m", "init")

            cvwrite("src/app.py")             # a rule claims it
            cvwrite("lib/new_package.py")     # the reported failure: nothing claims it
            cvwrite("docs/notes.md")          # excluded out loud
            cvwrite("pubspec.lock")           # generated output, excluded by default
            r = vfy("--coverage")
            check("a changed source file matching no glob is reported UNCHECKED",
                  "UNCHECKED" in r.stdout and "lib/new_package.py" in r.stdout)
            check("a path a rule DOES claim is not reported as unchecked",
                  "lib/new_package.py" in r.stdout and "src/app.py" not in r.stdout)
            check("a path listed under unchecked-ok is not reported as unchecked",
                  "lib/new_package.py" in r.stdout and "docs/notes.md" not in r.stdout)
            check("generated output is excluded without anyone listing it",
                  "lib/new_package.py" in r.stdout and "pubspec.lock" not in r.stdout)
            try:
                cov = json.loads(vfy("--coverage", "--porcelain").stdout)
            except ValueError:
                cov = {}          # no machine-readable answer is a FAILED check, not a crash
            check("the three buckets are counted, not just the unchecked one",
                  cov.get("unchecked") == ["lib/new_package.py"] and cov.get("checked") == 1
                  and cov.get("excluded") == 2 and cov.get("rules") == 1
                  and cov.get("exclusions") == 2)
            check("the coverage report is a REPORT, not a gate — exit 0 with a path unchecked",
                  r.returncode == 0 and "never a block" in r.stdout)
            check("the report states what coverage itself cannot see (INV6)",
                  "tautology" in r.stdout and "have not" in r.stdout and "changed" in r.stdout)

            r = vfy()
            check("a passing verify run still names what nothing checked",
                  r.returncode == 0 and "all owed checks passed" in r.stdout
                  and "NOT CHECKED" in r.stdout and "lib/new_package.py" in r.stdout)
            r = vfy("--check")
            check("a GREEN --check says what it did not look at (green != nothing unverified)",
                  r.returncode == 0 and "evidence is newer than the change" in r.stdout
                  and "NOT CHECKED" in r.stdout and "lib/new_package.py" in r.stdout)
            check("an unchecked path does NOT make --check refuse (visibility, never blocking)",
                  r.returncode == 0 and "VERIFY REFUSED" not in r.stdout)
            quiet = cvcommit()                                  # nothing staged: no accusation
            cvgit("add", "--", "lib/new_package.py", "docs/notes.md")
            r = cvcommit()
            check("the commit note names the STAGED path no rule checks",
                  "STAGED FILE NO RULE CHECKS" in r.stdout and "lib/new_package.py" in r.stdout
                  and "docs/notes.md" not in r.stdout)
            check("the commit note is stated, never blocked",
                  "NO RULE CHECKS" in r.stdout and not denied(r) and r.returncode == 0)
            check("the commit note states what it cannot see (INV6)",
                  "NO RULE CHECKS" in r.stdout and "commit -a" in r.stdout
                  and "silence here is not evidence" in r.stdout.lower())
            # the quiet case, asserted against a sibling that DID fire — a bare absence would pass
            # against code that never implemented the note at all
            check("an empty index accuses nobody (the note is about what the commit carries)",
                  "NO RULE CHECKS" in r.stdout and "NO RULE CHECKS" not in quiet.stdout)

            r = gl(cv, "status")
            check("status names the unchecked set every session",
                  "COVERAGE" in r.stdout and "UNCHECKED 1" in r.stdout
                  and "lib/new_package.py" in r.stdout)
            check("status states the write guard is an ALLOWLIST that denies the unnamed",
                  "ALLOWLIST" in r.stdout and "denied WITHOUT being named" in r.stdout)
            check("status names deploy verbs as the one DENYLIST, and says the unlisted run",
                  "DENYLIST" in r.stdout and "6 verb(s) blocked" in r.stdout
                  and "nobody listed is NOT blocked" in r.stdout)
            check("status states what NONE of the rails can see (INV6)",
                  "NONE of this sees" in r.stdout and "MCP tool" in r.stdout
                  and "has not CHANGED" in r.stdout)

            # The reported behaviour must be the ENFORCED behaviour: `status` keeps its own copy of
            # the deploy verbs, and a status line that understates a rail's reach is worse than none.
            src_gl = open(os.path.join(SRC_GAME_LOOP, "bin", "game_loop")).read()
            src_gd = open(os.path.join(SRC_GAME_LOOP, "bin", "guard-writes-impl.sh")).read()
            def pick(s, pat):
                m = _re.search(pat, s, _re.S)
                return _re.findall(r'"([^"]+)"', m.group(1)) if m else []

            check("the deploy verbs status reports are the ones the guard enforces",
                  pick(src_gl, r"DEPLOY_VERB_DEFAULTS = \[(.*?)\]")
                  == pick(src_gd, r"defaults = \[(.*?)\]") != [])

            # The existing gate must be untouched: a rule whose file changed AFTER its last green
            # run still refuses the commit. Coverage adds a report, it does not soften a check.
            now = _time.time()
            os.utime(os.path.join(cv, "src", "app.py"), (now + 60, now + 60))
            r = vfy("--check")
            check("existing owed-check behaviour is unchanged — a stale rule still REFUSES",
                  r.returncode == 1 and "VERIFY REFUSED" in r.stdout and "src/**" in r.stdout)
            check("and the stale refusal still blocks the commit",
                  denied(cvcommit()))

            # A FRESH INSTALL: templates/verify.yaml ships empty on purpose, because game_loop does
            # not know your project. Everything is unchecked — which must be SAID, and must not cost
            # the user their first commit. "Unlisted ⇒ refused" is the regression this repo already
            # fixed once, and INV5 forbids a guard that blocks its own fix.
            cvyaml("")
            r = vfy()
            check("an EMPTY manifest says NOTHING IS CHECKED, not merely 'nothing owed'",
                  r.returncode == 0 and "nothing owes a check" in r.stdout
                  and "NOTHING IS CHECKED" in r.stdout)
            r = vfy("--coverage")
            check("an empty manifest reports its own emptiness as a coverage fact",
                  "NO RULES AT ALL" in r.stdout and "not the same thing as safe" in r.stdout
                  and "lib/new_package.py" in r.stdout)
            r = vfy("--check")
            check("an empty manifest refuses nothing (--check stays a no-op)",
                  r.returncode == 0 and "VERIFY REFUSED" not in r.stdout)
            r = cvcommit()
            check("a fresh install's first commit is NOT blocked by an empty manifest",
                  not denied(r) and r.returncode == 0)
            check("and that commit is told, out loud, that nothing checked it",
                  "NOTHING IN THIS COMMIT IS CHECKED" in r.stdout
                  and "no rules" in r.stdout.lower())
            r = gl(cv, "status")
            check("status calls an empty manifest what it is, without calling it safe",
                  "NO RULES in .game_loop/verify.yaml" in r.stdout
                  and "not the same thing as safe" in r.stdout)
        finally:
            shutil.rmtree(cv, ignore_errors=True)

        # A report that cannot be read must say UNKNOWN. Degrading to a clean-looking line would
        # recreate the exact failure — a rail silent where it is blind.
        cu = make_sandbox()
        try:
            with open(os.path.join(cu, ".game_loop", "bin", "verify"), "w") as f:
                f.write("#!/usr/bin/env python3\nimport sys\nsys.stderr.write('boom\\n')\n"
                        "sys.exit(3)\n")
            r = gl(cu, "status")
            check("status reports UNKNOWN coverage, not clean, when the report cannot be read",
                  "UNREADABLE" in r.stdout and "UNKNOWN, not clean" in r.stdout)
        finally:
            shutil.rmtree(cu, ignore_errors=True)
        # #28: what a change owes, and whether the evidence is newer than the change, are facts
        # about a TREE — its files, their mtimes, its verified.json. The hook is registered as
        # "$CLAUDE_PROJECT_DIR"/.game_loop/bin/guard-writes.sh, so it always runs out of the MAIN
        # checkout, and a commit made in a git worktree was gated on the main checkout's record and
        # the main checkout's files. Wrong in both directions: finished, independently verified work
        # was refused because an unrelated tree was mid-something-else; and — the actual defect — a
        # worktree could commit a gated change while the main record sat green from a run that never
        # saw those files, defeating the gate's whole premise with evidence about a different tree.
        # Every guard() call below names the MAIN sandbox as the project, reproducing the live shape
        # exactly: the hook belongs to the main tree, the commit happens somewhere else.
        print("write guard (the commit gate follows the tree the commit lands in — #28):")
        import time as _t
        wmain = make_sandbox()
        try:
            def wgit(cwd, *args):
                return subprocess.run(["git", "-c", "user.email=t@example.invalid",
                                       "-c", "user.name=tester", "-c", "commit.gpgsign=false",
                                       "-c", "init.defaultBranch=main", *args],
                                      cwd=cwd, capture_output=True, text=True)

            def wverify(tree):
                """The REAL verify, run IN a tree — the only thing that makes that tree's record
                green, and it writes the record beside itself."""
                return subprocess.run([os.path.join(tree, ".game_loop", "bin", "verify")],
                                      cwd=tree, capture_output=True, text=True, env=_env())

            def _wpayload(cwd, sid=None):
                return {"tool_name": "Bash", "cwd": cwd, "session_id": sid or "",
                        "tool_input": {"command": "git commit -m x"}}

            def wcommit(cwd, sid=None):
                return guard(wmain, _wpayload(cwd, sid), sid=sid)

            def wallowed(cwd, sid=None):
                """The permissive half of the commit gate, with the second bit (#41): a commit this
                gate ALLOWS is silence, and so is a gate that never ran."""
                return allowed(wmain, _wpayload(cwd, sid), sid=sid)

            def wwrite(tree, rel, when):
                """Write a file and PIN its mtime: staleness here is a comparison against a recorded
                timestamp, and a test that depends on wall-clock ordering is a flaky test."""
                p = os.path.join(tree, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(rel + "\n")
                os.utime(p, (_t.time() + when, _t.time() + when))
                return p

            # A rule whose command PASSES, so a tree can be made genuinely green. The worktree and
            # the nested repo are kept out of the main tree's own status, or each tree would owe the
            # other's files; verified.json is runtime state and must not travel through git.
            with open(os.path.join(wmain, ".game_loop", "verify.yaml"), "w") as f:
                f.write('"*.txt":\n  - "true"\n')
            with open(os.path.join(wmain, ".gitignore"), "w") as f:
                f.write("wt/\nnested/\n.game_loop/verified.json\n")
            wgit(wmain, "init", "-q")
            wgit(wmain, "add", "-A")
            wgit(wmain, "commit", "-q", "-m", "init")
            wgit(wmain, "worktree", "add", "-q", "wt", "-b", "wtwork")
            wt = os.path.join(wmain, "wt")
            check("the worktree is real, and carries its own runnable .game_loop",
                  os.access(os.path.join(wt, ".game_loop", "bin", "verify"), os.X_OK)
                  and os.path.isdir(os.path.join(wt, ".git")) is False)

            # FALSE REFUSAL: worktree verified, main tree mid-something-else. Observed live.
            wwrite(wt, "work.txt", -5)
            wverify(wt)
            wwrite(wmain, "unrelated.txt", +5)
            r = wcommit(wmain)
            check("the main tree is genuinely stale, and refuses its own commit (the control)",
                  denied(r) and "VERIFY REFUSED" in r.stdout and "unrelated.txt" in r.stdout)
            check("...and a single-tree refusal says NOTHING about trees — the common path is "
                  "unchanged, which is what makes the worktree line meaningful",
                  "THIS COMMIT LANDS IN" not in r.stdout)
            check("a verified worktree commit is allowed while the main tree is stale (#28)",
                  wallowed(wt))

            # FALSE PASS — the defect. Main green from a run that never saw the worktree's files.
            os.utime(os.path.join(wmain, "unrelated.txt"), (_t.time() - 5, _t.time() - 5))
            wverify(wmain)
            check("the main tree passes once its own checks have run (the control)",
                  wallowed(wmain))
            wwrite(wt, "late.txt", +5)
            r = wcommit(wt)
            check("a STALE worktree commit is refused though the main record is green (#28)",
                  denied(r) and "VERIFY REFUSED" in r.stdout and "late.txt" in r.stdout)
            # #35: it denied correctly but told an agent to run a RELATIVE command without saying
            # where. With N worktrees exactly one of them clears the gate, and guessing wrong runs
            # verify in a tree that was already green — success, retry, same refusal, no new
            # information. The gate knew the answer; it was withholding it.
            check("...and the refusal NAMES the tree the commit lands in, not just the file",
                  wt in r.stdout and "THIS COMMIT LANDS IN" in r.stdout)
            check("...and gives an ABSOLUTE verify path, so there is nothing left to guess",
                  os.path.join(wt, ".game_loop", "bin", "verify") in r.stdout)
            # denied() is asserted again on purpose: without it this reads as satisfied by a commit
            # that was never refused at all, which is exactly the broken behaviour.
            check("and the refusal names the worktree's file, not the main tree's",
                  denied(r) and "unrelated.txt" not in r.stdout)

            # A tree the guard can NAME but cannot CHECK. Borrowing the project's record here would
            # report confidence about files that record never saw, which is the exact failure above.
            os.remove(os.path.join(wt, "late.txt"))
            wverify(wt)
            nested = os.path.join(wmain, "nested")
            os.makedirs(nested, exist_ok=True)
            wgit(nested, "init", "-q")
            r = wcommit(nested)
            check("a commit landing in a tree with no game_loop is refused, not borrowed (#28)",
                  denied(r) and "carries no game_loop" in r.stdout
                  and "--no-verify" in r.stdout and nested in r.stdout)

            # The edited set stays SESSION-wide — one session is one session however many trees it
            # works in — but the INDEX it is compared against must be the committing tree's, or the
            # two sets describe different worlds and every worktree file reads as excess.
            sid = "sess-wt-blast"
            guard(wmain, {"tool_name": "Write", "session_id": sid,
                          "tool_input": {"file_path": os.path.join(wt, "lib", "mine.dart")}},
                  sid=sid)
            for rel in ("lib/mine.dart", "lib/swept.dart"):
                wwrite(wt, rel, -5)
            wgit(wt, "add", "--", "lib/mine.dart", "lib/swept.dart")
            r = wcommit(wt, sid=sid)
            check("the blast-radius warning reads the COMMITTING tree's index (#28)",
                  not denied(r)
                  and "COMMIT INCLUDES 1 FILE THIS SESSION NEVER EDITED" in r.stdout
                  and "swept.dart" in r.stdout and "mine.dart" not in r.stdout)
            check("the session's edited set is one set, written beside the session's state",
                  os.path.exists(os.path.join(wmain, ".game_loop", "sessions", sid, "edited.txt")))

            # REGRESSION GUARDS, and they pass in BOTH states by construction — that IS the claim:
            # resolving the tree changed nothing for a commit in the project itself.
            wwrite(wmain, "second.txt", +5)
            r = wcommit(wmain)
            check("an ordinary in-project commit is still gated on the project's own record",
                  denied(r) and "VERIFY REFUSED" in r.stdout and "second.txt" in r.stdout)
            os.utime(os.path.join(wmain, "second.txt"), (_t.time() - 5, _t.time() - 5))
            wverify(wmain)
            check("and passes again once the project's own checks have run",
                  wallowed(wmain))
        finally:
            shutil.rmtree(wmain, ignore_errors=True)

        # #29: the blast-radius set is scoped to the SESSION, deliberately and correctly — which is
        # exactly why it can never hold what a SIBLING session wrote on a branch. The moment this
        # session integrates that work, `git merge` brings the files in and every one reads as
        # excess. Observed live across ~14 integration commits: the warning fired on 8, naming only
        # legitimate files. A merge-ONLY session is already silent (no recorded edits ⇒ no
        # accusation); the broken case is the MIXED session — a few edits of its own PLUS merges —
        # which is an orchestrator's normal shape, and it is the shape reproduced below.
        # The fix is not a wider exemption list. It is a single-use, logged declaration that names
        # REFS and lets game_loop recompute the files itself, so a supplied list of filenames — the
        # one thing a model can write for free and nothing can check — is never accepted.
        print("write guard (a commit's provenance, declared by REF — #29):")
        at = make_sandbox()
        try:
            with open(os.path.join(at, ".game_loop", "verify.yaml"), "w") as f:
                f.write("")            # empty: the owed-checks gate must not deny and swallow the note
            at_state_f = os.path.join(at, ".game_loop", "sessions", "sess-attr", "state.json")
            at_log_f = os.path.join(at, ".game_loop", "log.jsonl")

            def atgit(*args):
                return subprocess.run(["git", "-c", "user.email=t@example.invalid",
                                       "-c", "user.name=tester", "-c", "commit.gpgsign=false",
                                       "-c", "init.defaultBranch=main", *args],
                                      cwd=at, capture_output=True, text=True)

            def atgl(*args, cwd=None, sid="sess-attr"):
                """game_loop with its CWD IN THE TREE. `attribute` resolves the tree from the cwd,
                the same way the commit gate resolves the tree a commit lands in (#28)."""
                return subprocess.run([os.path.join(at, ".game_loop", "bin", "game_loop"), *args],
                                      cwd=cwd or at, capture_output=True, text=True,
                                      env=_env(sid=sid))

            def atwrite(rel):
                p = os.path.join(at, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(rel + "\n")

            def atedit(rel, sid="sess-attr"):
                """The REAL guard on a real Write — the only thing that records a session edit."""
                return guard(at, {"tool_name": "Write", "session_id": sid,
                                  "tool_input": {"file_path": os.path.join(at, rel)}}, sid=sid)

            def atcommit(sid="sess-attr"):
                return guard(at, {"tool_name": "Bash", "session_id": sid, "cwd": at,
                                  "tool_input": {"command": "git commit -m x"}}, sid=sid)

            def atlog():
                return open(at_log_f).read() if os.path.exists(at_log_f) else ""

            def atattributed():
                """The live declarations, fail-soft: a missing state file is 'none', not a crash —
                these run against code that may never have written one."""
                try:
                    with open(at_state_f) as f:
                        return json.load(f).get("attributed") or []
                except (OSError, ValueError):
                    return []

            # Running the real CLI in this tree leaves __pycache__ beside its modules; ignoring it
            # keeps `git add -A` describing the fixture rather than the interpreter.
            with open(os.path.join(at, ".gitignore"), "w") as f:
                f.write("__pycache__/\n")
            atgit("init", "-q")
            atwrite("README.md")
            atgit("add", "-A")
            atgit("commit", "-q", "-m", "init")
            # A SIBLING session's work, on a real branch: three files THIS session never wrote.
            atgit("checkout", "-q", "-b", "crawler/auth")
            for rel in ("lib/auth.dart", "lib/token.dart", "lib/session_store.dart"):
                atwrite(rel)
            atgit("add", "-A")
            atgit("commit", "-q", "-m", "the crawler's work")
            atgit("checkout", "-q", "main")
            # THE MIXED SHAPE: land the sibling's branch, and edit one file of our own on top.
            atgit("merge", "--no-commit", "--no-ff", "crawler/auth")
            atwrite("lib/mine.dart")
            atedit("lib/mine.dart")
            atgit("add", "--", "lib/mine.dart")

            r = atcommit()
            # THE CONTROL, and it passes in BOTH states by construction — that is the point of it:
            # this is the reported defect, reproduced, and the fix must not change it until a
            # declaration exists. It proves the fixture is really the broken shape, nothing more.
            check("the reported defect, reproduced: a merge's files read as this session's excess",
                  not denied(r)
                  and "COMMIT INCLUDES 3 FILES THIS SESSION NEVER EDITED" in r.stdout
                  and "lib/auth.dart" in r.stdout and "lib/token.dart" in r.stdout
                  and "lib/mine.dart" not in r.stdout)
            check("and the warning now names the way out, by REF — a guard must not block its own "
                  "fix (INV5)",
                  "LANDING WORK A SIBLING SESSION PRODUCED ON A BRANCH" in r.stdout
                  and "game_loop attribute --merge <ref> [--merge <ref> ...]" in r.stdout)

            # THE KEYSTONE. A ref is checkable and the recomputation IS the check, so a ref that does
            # not resolve is refused outright — the same shape as `claim --read` refusing a path that
            # is not there. Asserted on the words, never on a bare non-zero exit: an unknown
            # subcommand also exits non-zero, and would "pass" this against code with no verb at all.
            r = atgl("attribute", "--merge", "crawler/does-not-exist", "--reason", "landing it")
            check("an attribution naming a ref that does not resolve is REFUSED, in words",
                  r.returncode != 0 and "REFUSES this declaration" in r.stderr
                  and "'crawler/does-not-exist' does not resolve to a commit" in r.stderr)
            # The precise thing this verb refuses to accept: a FILENAME. A list of paths is the
            # plausible string a model produces for free; it is not a ref, so it does not resolve.
            r = atgl("attribute", "--merge", "lib/auth.dart", "--reason", "landing it")
            check("a FILENAME handed to --merge is refused — this verb takes refs, not paths",
                  r.returncode != 0
                  and "'lib/auth.dart' does not resolve to a commit" in r.stderr)

            r = atgl("attribute", "--merge", "crawler/auth",
                     "--reason", "landing the crawler's auth work")
            check("an attribution naming a real ref recomputes that ref's file set itself",
                  r.returncode == 0 and "ATTRIBUTED" in r.stdout
                  and "crawler/auth: 3 file(s)" in r.stdout
                  and "only the refs" in r.stdout)
            stored = (atattributed() or [{}])[0]
            check("what is stored is the RECOMPUTED set, keyed to the ref that produced it",
                  len(atattributed()) == 1 and stored.get("refs") == ["crawler/auth"]
                  and sorted(stored.get("files") or []) == ["lib/auth.dart",
                                                            "lib/session_store.dart",
                                                            "lib/token.dart"]
                  and stored.get("uses_left") == 1)
            # A refusal must leave nothing behind to be spent later. Asserted against a state that
            # already HOLDS one declaration, so "nothing was added" is a real observation rather
            # than the empty truth it would be against a tool with no such verb at all.
            r = atgl("attribute", "--merge", "crawler/also-not-real", "--reason", "landing it")
            check("a refused declaration adds nothing to the state it was refused against",
                  r.returncode != 0 and len(atattributed()) == 1
                  and atattributed()[0].get("refs") == ["crawler/auth"])
            check("the declaration is permanent in the log, with its refs and its reason",
                  '"kind": "attribute"' in atlog() and '"crawler/auth"' in atlog()
                  and "landing the crawler's auth work" in atlog())

            # A file in NEITHER bucket: not written through Write/Edit, not carried by any named ref.
            # This is the finding the old warning buried among ten legitimate ones.
            atwrite("lib/nobody_wrote_this.dart")
            atgit("add", "-A")
            r = atcommit()
            check("attributed files drop out of the warning entirely",
                  "lib/auth.dart" not in r.stdout and "lib/token.dart" not in r.stdout
                  and "lib/session_store.dart" not in r.stdout)
            check("a file in NEITHER bucket is still named, and is now the ONLY thing named",
                  "COMMIT INCLUDES 1 FILE NOTHING ACCOUNTS FOR" in r.stdout
                  and "lib/nobody_wrote_this.dart" in r.stdout
                  and "lib/mine.dart" not in r.stdout)
            check("the note says what it accounted for and why that is STRICTER, not quieter",
                  "3 other staged files came in with an attributed merge" in r.stdout
                  and "crawler/auth" in r.stdout
                  and "landing the crawler's auth work" in r.stdout
                  and "STRICTLY the unexplained ones" in r.stdout)
            check("the WHAT-THIS-SET-SEES paragraph GAINED the attributed case, losing nothing "
                  "(INV6)",
                  "WHAT THIS SET SEES" in r.stdout and "Write/Edit/NotebookEdit" in r.stdout
                  and "game_loop attribute --merge <ref>` declaration accounts for" in r.stdout
                  and "silence here is not evidence" in r.stdout.lower())
            check("a provenance-aware warning still NEVER blocks the commit",
                  "NOTHING ACCOUNTS FOR" in r.stdout and not denied(r) and r.returncode == 0)
            check("the SPEND is its own permanent record, naming the refs it was spent on",
                  '"attributed_merge"' in atlog() and '"crawler/auth"' in atlog())

            # SINGLE-USE, exactly like `authorize`: one declaration buys one commit. The spend count
            # is asserted, not just the message — the message alone reads the same against code that
            # never had the verb, and a test that cannot fail is worthless.
            atwrite("lib/second_nobody.dart")
            atgit("add", "-A")
            r2 = atcommit()
            spends = [ln for ln in atlog().splitlines() if '"attributed_merge"' in ln]
            check("the declaration is single-use — one spend, and the SECOND commit is not covered",
                  len(spends) == 1
                  and "NOTHING ACCOUNTS FOR" not in r2.stdout
                  and "COMMIT INCLUDES 5 FILES THIS SESSION NEVER EDITED" in r2.stdout
                  and "lib/auth.dart" in r2.stdout)
            # A REGRESSION GUARD that passes in BOTH states by construction — that IS the claim:
            # with nothing declared, this check says exactly what it said before.
            check("with no live attribution the warning is word-for-word the one that shipped before",
                  "attributed merge" not in r2.stdout
                  and "'git add -A' swept it in" in r2.stdout
                  and "git restore --staged <path>" in r2.stdout
                  and "silence here is not evidence" in r2.stdout.lower())

            # Git must never throw out of this. Every degenerate tree — no git at all, unrelated
            # histories — becomes a STATED refusal, never a traceback out of a gate.
            ng = make_sandbox()
            try:
                def nggl(*args):
                    return subprocess.run([os.path.join(ng, ".game_loop", "bin", "game_loop"),
                                           "attribute", *args], cwd=ng, capture_output=True,
                                          text=True, env=_env(sid="sess-nogit"))

                def nggit(*args):
                    return subprocess.run(["git", "-c", "user.email=t@example.invalid",
                                           "-c", "user.name=tester", "-c", "commit.gpgsign=false",
                                           "-c", "init.defaultBranch=main", *args],
                                          cwd=ng, capture_output=True, text=True)

                r = nggl("--merge", "main", "--reason", "landing it")
                check("attribute in a tree with no git refuses in words, never with a traceback",
                      r.returncode != 0 and "is not a git tree" in r.stderr
                      and "Traceback" not in r.stderr and "Traceback" not in r.stdout)
                nggit("init", "-q")
                with open(os.path.join(ng, "a.txt"), "w") as f:
                    f.write("a\n")
                nggit("add", "-A")
                nggit("commit", "-q", "-m", "init")
                nggit("checkout", "-q", "--orphan", "stranger")
                with open(os.path.join(ng, "b.txt"), "w") as f:
                    f.write("b\n")
                nggit("add", "-A")
                nggit("commit", "-q", "-m", "unrelated")
                nggit("checkout", "-q", "main")
                r = nggl("--merge", "stranger", "--reason", "landing it")
                check("a ref sharing no history with HEAD is refused, saying there is nothing to "
                      "recompute",
                      r.returncode != 0 and "share no merge-base" in r.stderr
                      and "Traceback" not in r.stderr and "Traceback" not in r.stdout)
            finally:
                shutil.rmtree(ng, ignore_errors=True)
        finally:
            shutil.rmtree(at, ignore_errors=True)
        # #30: install.sh seeds the user-owned files ONLY IF ABSENT, and templates/verify.yaml ships
        # with no rules at all. Into a LINKED WORKTREE of a project whose .game_loop/ the user has
        # gitignored themselves, nothing crosses — so the install seeds a BLANK manifest, and a blank
        # manifest owes nothing: the commit gate goes on reporting success while checking nothing.
        # Present and different is the worst shape a rail can take, and it arrives in silence.
        #
        # (The DEFAULT install does not have this problem, and the tests below build both shapes to
        # keep that honest: install.sh writes an inner .game_loop/.gitignore listing only RUNTIME
        # state, so config.json / INVARIANTS.md / verify.yaml are TRACKED and a worktree inherits
        # them correctly. The gap is real only where the user gitignored the whole directory.)
        #
        # A linked worktree is a second working copy of ONE project, not a new project, and git
        # already knows which case is which — so the project's own files are the right default there
        # and no one has to know a flag. Where git cannot connect two trees, --same-as says it by hand.
        print("install into a linked worktree carries the PROJECT's rules, not blank ones (#30):")
        adopt = tempfile.mkdtemp(prefix="gameloop-adopt-")
        try:
            def agit(cwd, *args):
                return subprocess.run(["git", "-c", "user.email=t@example.invalid",
                                       "-c", "user.name=tester", "-c", "commit.gpgsign=false",
                                       "-c", "init.defaultBranch=main", *args],
                                      cwd=cwd, capture_output=True, text=True)

            def install(*args):
                """The REAL installer, through its real interface — the thing being fixed."""
                return subprocess.run([os.path.join(REPO, "install.sh"), *args],
                                      capture_output=True, text=True, env=_env())

            def read(*parts):
                with open(os.path.join(*parts)) as f:
                    return f.read()

            def mkproject(name, rules=True):
                """A real git repo that gitignores its WHOLE .game_loop/ — the one non-default
                choice under which a linked worktree inherits no rules at all."""
                p = os.path.join(adopt, name)
                os.makedirs(p)
                agit(p, "init", "-q")
                with open(os.path.join(p, ".gitignore"), "w") as f:
                    f.write(".game_loop/\n")
                with open(os.path.join(p, "README.md"), "w") as f:
                    f.write("x\n")
                if rules:
                    install(p)
                    with open(os.path.join(p, ".game_loop", "verify.yaml"), "a") as f:
                        f.write('"src/**":\n  - "make the-projects-own-check"\n')
                    with open(os.path.join(p, ".game_loop", "INVARIANTS.md"), "a") as f:
                        f.write("\n## INV9 — the project's own north star\n")
                    # install.sh stamps a VERSION, which makes `status` reach for the network to
                    # compare shas. This suite makes no network call: turn the courtesy check off.
                    cf = os.path.join(p, ".game_loop", "config.json")
                    with open(cf) as f:
                        c = json.load(f)
                    c["update_check"] = False
                    with open(cf, "w") as f:
                        json.dump(c, f, indent=2)
                        f.write("\n")
                agit(p, "add", "-A")
                agit(p, "commit", "-q", "-m", "init")
                return p

            def worktree_of(p, name):
                wt = os.path.join(adopt, name)
                agit(p, "worktree", "add", "-q", wt, "-b", name)
                return wt

            mainco = mkproject("mainco")
            wt = worktree_of(mainco, "wt")
            # PREMISE, not a behaviour claim: it holds in both states, and it is what makes the rest
            # of this block a test of anything. If a worktree DID inherit the rules there is no bug.
            check("the premise: a worktree of a .game_loop-gitignoring project inherits no rules",
                  os.path.isdir(os.path.join(wt, ".git")) is False
                  and not os.path.exists(os.path.join(wt, ".game_loop")))

            r = install(wt)
            check("installing into a linked worktree adopts the project's verify.yaml, not the "
                  "blank template",
                  r.returncode == 0 and "adopted .game_loop/verify.yaml" in r.stdout
                  and "make the-projects-own-check" in read(wt, ".game_loop", "verify.yaml"))
            # #69: THE RULES CAME AND THE SCRIPT THEY INVOKE DID NOT, which blocked three Crawlers.
            # verify.yaml can name a command the PROJECT wrote, living in .game_loop/bin/ because
            # that is where the rule points at it. Adopting the rules without it leaves a gate that
            # cannot run -- and since #66 that is correctly reported as "could not tell", so an
            # orchestrator correctly refuses to dispatch and NO tree can satisfy both. Every gate
            # right, no forward path, which is the worst shape a correct fix can have.
            #
            # I shipped #66's verdict half and closed the issue whose own title named this half too.
            _proj = os.path.join(mainco, ".game_loop", "bin", "check-project-thing")
            with open(_proj, "w") as f:
                f.write("#!/usr/bin/env bash\nexit 0\n")
            os.chmod(_proj, 0o755)
            _wt2 = worktree_of(mainco, "wt2")
            install(_wt2)
            _carried = os.path.join(_wt2, ".game_loop", "bin", "check-project-thing")
            check("adopting a harness carries the project's OWN bin/ scripts, not only its rules — a "
                  "rule naming a command absent from this tree is a gate that cannot run",
                  os.path.isfile(_carried) and os.access(_carried, os.X_OK))
            check("...and the adopted tree therefore reports CLEAN rather than undetermined, which "
                  "is the difference between an orchestrator dispatching and refusing forever",
                  json.loads(gl(_wt2, "worktree", "--porcelain").stdout)["status"] == "clean")
            check("...while game_loop's OWN shipped binary is still the payload's, so adopting a "
                  "parent can never install its older harness over this newer one",
                  read(_wt2, ".game_loop", "bin", "game_loop")
                  == read(mainco, ".game_loop", "bin", "game_loop"))
            os.remove(_proj)

            check("...and its INVARIANTS.md, so the tree's north star is the project's",
                  "## INV9 — the project's own north star" in read(wt, ".game_loop",
                                                                  "INVARIANTS.md"))
            with open(os.path.join(mainco, ".game_loop", "config.json"), "rb") as f:
                pcfg = f.read()
            with open(os.path.join(wt, ".game_loop", "config.json"), "rb") as f:
                wcfg = f.read()
            check("...and its config.json BYTE-FOR-BYTE — no rename of project_name to the "
                  "worktree's directory, which would manufacture the drift this just prevented",
                  pcfg == wcfg)
            rv = subprocess.run([os.path.join(wt, ".game_loop", "bin", "verify"), "--check"],
                                cwd=wt, capture_output=True, text=True, env=_env())
            check("and the adopted tree's gate has rules to enforce, where a blank one owed nothing",
                  "no rules in .game_loop/verify.yaml" not in rv.stdout)

            r = gl(wt, "worktree", "--porcelain")
            d = json.loads(r.stdout)
            check("an adopted worktree reports clean — and clean is the ONLY verdict that exits 0",
                  r.returncode == 0 and d["status"] == "clean" and not d["rules"]["drifted"]
                  and d["rules"]["matched"] == ["config.json", "INVARIANTS.md", "verify.yaml"]
                  and d["owned_files"]["matched"] == ["config.json", "INVARIANTS.md",
                                                      "verify.yaml", "LEDGER.md"])
            # #38 — `harness` used to be computed over OWNED_FILES only, and bin/ is not in that
            # set. So `harness.drifted == []` meant "the same RULES" while the field name promised
            # something strictly larger; a downstream orchestrator told its users a spawn was
            # "verified by the harness itself", which was true and narrower than it read. Widened
            # rather than renamed: "are these two trees running the same harness" is a legitimate
            # question a caller had no way to ask, and widening can only report MORE drift.
            check("...and `harness` now actually contains the harness — the owned files AND the code",
                  any(n.startswith("bin/") for n in d["harness"]["matched"])
                  and set(d["owned_files"]["matched"]) <= set(d["harness"]["matched"]))
            check("...and the code comparison is its own answer, so a caller can ask only that",
                  d["code"]["matched"] and not d["code"]["drifted"]
                  and all(not n.startswith("bin/") for n in d["code"]["matched"]))
            check("...and `compares` lists exactly what was looked at, so reach is never inferred "
                  "from a field name again",
                  sorted(d["compares"]) == ["code", "local_override", "owned_files", "rules"]
                  and all(n.startswith("bin/") for n in d["compares"]["code"])
                  # local_override earns its place here: it is not a rule file and not owned, but
                  # its keys MERGE WITH UNION semantics, so two trees can enforce different
                  # EFFECTIVE policy while every compared file is byte-identical. A consumer that
                  # reads `compares` to know the reach of a verdict has to see it listed.
                  and d["compares"]["local_override"] == ["config" + ".local.json"])
            # Drifting one SCRIPT must change the verdict — otherwise the widening is decorative.
            _script = os.path.join(wt, ".game_loop", "bin", "flair.py")
            with open(_script, "a") as f:
                f.write("\n# drifted\n")
            _cd = json.loads(gl(wt, "worktree", "--porcelain").stdout)
            check("a drifted harness SCRIPT is caught and named, with a status of its own — two "
                  "trees can match on every owned file and still not enforce them the same way",
                  _cd["status"] == "code-drifted" and "flair.py" in _cd["code"]["drifted"]
                  and not _cd["rules"]["drifted"])
            with open(_script) as f:
                _restored = f.read().replace("\n# drifted\n", "")
            with open(_script, "w") as f:
                f.write(_restored)

            # TWO questions, two answers. A ledger of findings is a record of ONE tree's work, so it
            # is owned but is not a gate: two trees keeping different ones is what they are for.
            # Collapsing that into "the harnesses differ" would make the real signal — differing
            # RULES — arrive wrapped in noise that a spawn path learns to ignore.
            with open(os.path.join(wt, ".game_loop", "LEDGER.md"), "a") as f:
                f.write("\n- VERIFIED: something this tree alone looked into\n")
            r = gl(wt, "worktree", "--porcelain")
            d = json.loads(r.stdout)
            check("a differing LEDGER.md is notes-drifted, NOT drifted — the rules still match",
                  d["status"] == "notes-drifted" and d["rules"]["drifted"] == []
                  and d["harness"]["drifted"] == ["LEDGER.md"])
            check("...and it gets its own exit code, so a spawn can warn where it would not block",
                  r.returncode == 3)
            r = gl(wt, "status")
            check("...and status says so as a note rather than as a finding",
                  "notes differ" in r.stdout and "RULES MATCH" in r.stdout
                  and "RULES DIFFER" not in r.stdout)

            # Now drift a RULE, and make sure both renderings say the same thing about the same files.
            with open(os.path.join(wt, ".game_loop", "verify.yaml"), "a") as f:
                f.write('"docs/**":\n  - "only-in-this-tree"\n')
            r = gl(wt, "status")
            check("status NAMES the drifted rule file in a linked worktree",
                  "WORKTREE" in r.stdout and "RULES DIFFER" in r.stdout
                  and ".game_loop/verify.yaml" in r.stdout
                  and str(mainco) in r.stdout)
            r = gl(wt, "worktree", "--porcelain")
            d = json.loads(r.stdout)
            check("worktree --porcelain names WHICH files drifted, not merely that something did",
                  d["status"] == "drifted" and d["rules"]["drifted"] == ["verify.yaml"]
                  and d["rules"]["matched"] == ["config.json", "INVARIANTS.md"])
            check("...and a rule drift outranks the notes drift it is reported alongside",
                  sorted(d["harness"]["drifted"]) == ["LEDGER.md", "verify.yaml"])
            check("...and a drifted tree exits 1 — distinguishable from clean without parsing prose",
                  r.returncode == 1)

            # The three ways of NOT KNOWING, each named and each exit 2. An orchestrator that reads
            # "cannot determine" as "clean" is the silent-and-wrong failure this whole issue is about,
            # so none of them is allowed to share an exit code with a tree that was actually compared.
            r = gl(mainco, "worktree", "--porcelain")
            check("a main checkout reports not-a-worktree at exit 2, never a clean 0",
                  r.returncode == 2 and json.loads(r.stdout)["status"] == "not-a-worktree")

            r = install(wt)
            check("re-installing a drifted worktree keeps its files and says DRIFT out loud",
                  r.returncode == 0 and "DRIFT" in r.stdout
                  and "kept    .game_loop/verify.yaml" in r.stdout
                  and "only-in-this-tree" in read(wt, ".game_loop", "verify.yaml"))

            # A worktree of a project with NO harness. Seeding the templates here is exactly the
            # silent substitution, so it is refused — and refused BEFORE anything is written, because
            # a tree left holding half a harness is not a refusal.
            nogl = mkproject("nogl", rules=False)
            wtb = worktree_of(nogl, "wtnogl")
            r = install(wtb)
            check("installing into a linked worktree whose main checkout has NO harness is REFUSED",
                  r.returncode != 0 and "REFUSED" in r.stderr and "LINKED WORKTREE" in r.stderr
                  and nogl in r.stderr)
            check("...and the refusal writes nothing at all into the tree",
                  not os.path.exists(os.path.join(wtb, ".game_loop")))
            check("...and it names a way through itself, which a guard owes (INV5)",
                  "--fresh" in r.stderr and "--same-as" in r.stderr)
            r = install("--fresh", wtb)
            check("--fresh is that way through: it seeds the blank templates and says so",
                  r.returncode == 0 and "seeded  .game_loop/verify.yaml" in r.stdout)
            r = gl(wtb, "worktree", "--porcelain")
            check("a worktree whose main checkout has no harness says so at exit 2, not clean",
                  r.returncode == 2 and json.loads(r.stdout)["status"] == "no-parent-harness")

            # An ordinary project. This is the common path and it was already correct: these two
            # PASS IN BOTH STATES by construction, and that is the entire claim being made.
            plain = os.path.join(adopt, "plain")
            os.makedirs(plain)
            fresh = install(plain)
            check("a fresh install into an ordinary project still seeds the blank template",
                  fresh.returncode == 0 and "seeded  .game_loop/verify.yaml" in fresh.stdout
                  and "adopted" not in fresh.stdout)
            with open(os.path.join(plain, ".game_loop", "config.json")) as f:
                check("...and still renames project_name to the target's own directory",
                      json.load(f).get("project_name") == "plain")
            r = gl(plain, "worktree", "--porcelain")
            check("a tree that is no git repo at all degrades to a verdict, never a traceback",
                  r.returncode == 2 and json.loads(r.stdout)["status"] == "not-a-worktree"
                  and "Traceback" not in r.stderr)

            # --same-as: the same behaviour, stated by hand, for the trees git cannot connect.
            sib = os.path.join(adopt, "sibling")
            os.makedirs(sib)
            r = install("--same-as", mainco, sib)
            check("--same-as carries a project's rules into a tree that is NOT a linked worktree",
                  r.returncode == 0 and "adopted .game_loop/verify.yaml" in r.stdout
                  and "make the-projects-own-check" in read(sib, ".game_loop", "verify.yaml"))
            sib2 = os.path.join(adopt, "sibling2")
            os.makedirs(sib2)
            r = install("--same-as", nogl, sib2)
            check("--same-as refuses a checkout with no game_loop files rather than seeding blanks",
                  r.returncode != 0 and "REFUSED" in r.stderr
                  and not os.path.exists(os.path.join(sib2, ".game_loop")))
            r = install("--same-as", os.path.join(adopt, "no-such-tree"), sib2)
            check("--same-as on a path that does not exist fails loudly, and still writes nothing",
                  r.returncode != 0 and "no such directory" in r.stderr.lower()
                  and not os.path.exists(os.path.join(sib2, ".game_loop")))

            # The owned-file set has ONE home. An external tool that keeps its own copy goes wrong
            # silently the moment game_loop adds a file, so the set is published rather than guessed.
            r = gl(wt, "owned", "--porcelain")
            pub = json.loads(r.stdout)
            owned = pub["owned"]
            check("the owned-file set is readable from outside the process, so nothing hardcodes it",
                  r.returncode == 0
                  and [o["path"] for o in owned] == ["config.json", "INVARIANTS.md",
                                                     "verify.yaml", "LEDGER.md"])
            check("...and it is published as TWO named sets, so no caller has to guess which it "
                  "is answering",
                  pub["rule_files"] == ["config.json", "INVARIANTS.md", "verify.yaml"]
                  and pub["notes_files"] == ["LEDGER.md"]
                  and sorted(pub["rule_files"] + pub["notes_files"])
                  == sorted(o["path"] for o in owned))
            # `owned` is asserted to be the full four in both: `all()` over an empty list is True,
            # and a check an empty answer satisfies is a check that cannot fail.
            check("...install.sh seeds exactly that set — one list, not two that drift apart",
                  len(owned) == 4
                  and all(f"  seeded  .game_loop/{o['path']}" in fresh.stdout for o in owned))
            wd = json.loads(gl(wt, "worktree", "--porcelain").stdout)
            check("...and the drift verdict carries both sets too, so one call answers both questions",
                  len(owned) == 4 and wd["owned"] == owned
                  and wd["rule_files"] == pub["rule_files"]
                  and wd["notes_files"] == pub["notes_files"])
            # Empty == empty would satisfy this trivially, so both sides are pinned to the real set.
            check("...and the drift check compares them SEPARATELY, naming which set it means",
                  len(pub["rule_files"]) == 3 and len(owned) == 4
                  and sorted(wd["rules"]["matched"] + wd["rules"]["drifted"]
                             + wd["rules"]["unreadable"]) == sorted(pub["rule_files"])
                  and sorted(wd["owned_files"]["matched"] + wd["owned_files"]["drifted"]
                             + wd["owned_files"]["unreadable"])
                  == sorted(o["path"] for o in owned))
        finally:
            shutil.rmtree(adopt, ignore_errors=True)
    finally:
        shutil.rmtree(proj, ignore_errors=True)

    # #34: the hooks-live warning claims only what the probe supports, and names the remedy for the
    # HOST. Absence of the probe is a statement about the PROBE's lifetime, not the hook's — a
    # checkout that fired before probing shipped reads identically until its next Stop, so the wording
    # must say "no record", never "never fired". And the VSCode extension registers hooks at WINDOW
    # LOAD, so "start a new session" is the expensive remedy there and obscures the one that works.
    print("hooks-live warning (#34):")
    hp = make_sandbox()
    try:
        def status_with(entrypoint):
            env = _env(hp, sid="sess-hooks", CLAUDE_CODE_ENTRYPOINT=entrypoint)
            return subprocess.run([os.path.join(hp, ".game_loop", "bin", "game_loop"), "status"],
                                  capture_output=True, text=True, env=env).stdout

        probe = os.path.join(hp, ".game_loop", "probe", "stop-payload.json")
        if os.path.exists(probe):
            os.remove(probe)

        vs = status_with("claude-vscode")
        check("no probe → the warning fires", "HOOKS NOT LIVE" in vs)
        check("it says NO RECORD, not 'never fired' (the probe's lifetime, not the hook's)",
              "no record of the Stop gate firing" in vs and "never fired" not in vs)
        check("it states that limit out loud, not only in a comment (INV6)",
              "PROBE's lifetime" in vs)
        check("under the VSCode extension the remedy is the RELOAD, not a restart",
              "Reload the VSCode window" in vs)
        other = status_with("cli")
        check("under any other host the remedy is a new session",
              "Start a new session" in other and "Reload the VSCode window" not in other)
        check("the host is read from CLAUDE_CODE_ENTRYPOINT — TERM_PROGRAM is unset here and "
              "would detect nothing",
              "Reload the VSCode window" in status_with("claude-vscode")
              and "Reload the VSCode window" not in subprocess.run(
                  [os.path.join(hp, ".game_loop", "bin", "game_loop"), "status"],
                  capture_output=True, text=True,
                  env=_env(hp, sid="sess-hooks", TERM_PROGRAM="vscode")).stdout)

        os.makedirs(os.path.dirname(probe), exist_ok=True)
        with open(probe, "w") as f:
            f.write("{}")
        check("a recorded firing silences it", "HOOKS NOT LIVE" not in status_with("claude-vscode"))

        # #43 — REGISTERED, FIRED and LISTENING NOW are three different claims, and the check tested
        # only the second. A Stop hook that fired an hour ago and has since died left its probe on
        # disk, so `isfile` stayed true and the check stayed silent for the rest of the session. The
        # mtime was available and used nowhere.
        _stale = "STOP GATE MAY HAVE STOPPED"
        check("a FRESH probe stays silent — the run is healthy and must not be nagged",
              _stale not in status_with("cli"))

        # Age it past the slack while the session's own activity stays recent. Judged against THIS
        # RUN'S behaviour, not a constant: an idle session moves neither marker and triggers nothing.
        _old = time.time() - (3 * 3600)
        os.utime(probe, (_old, _old))
        _sd = os.path.join(hp, ".game_loop", "sessions", "sess-hooks")
        os.makedirs(_sd, exist_ok=True)
        for _f in (os.path.join(_sd, "state.json"), os.path.join(hp, ".game_loop", "log.jsonl")):
            if not os.path.exists(_f):
                with open(_f, "w") as fh:
                    fh.write("{}\n")
            os.utime(_f, None)                      # this session worked just now
        _said = status_with("cli")
        check("a probe far older than the session's own activity IS caught — a hook that died is "
              "not a quiet run",
              _stale in _said)
        check("...and it says both ages, so the reader can judge rather than trust the verdict",
              "min old" in _said and "min ago" in _said)
        check("...and states the limit it still has: fired then is not listening now (INV6)",
              "never that it will run at the next one" in _said)

        # PAIRED THE OTHER WAY: an IDLE session must not be accused. Nothing re-arms a hook while
        # idle, so age alone would flag every quiet run — the comparison is what makes it evidence.
        for _f in (os.path.join(_sd, "state.json"), os.path.join(hp, ".game_loop", "log.jsonl")):
            os.utime(_f, (_old - 60, _old - 60))
        check("an IDLE session whose work is as old as its probe is NOT accused — age alone would "
              "flag every quiet run",
              _stale not in status_with("cli"))
    finally:
        shutil.rmtree(hp, ignore_errors=True)

    # USAGE-LIMIT INERTNESS (reported: two agents burned to their session limit with no page, no
    # hard stop, and no restart when the window reset). One cause under all three symptoms: the
    # snapshot is fed ONLY by the statusline payload — `rate_limits` lives in no hook payload, which
    # was verified against the shipped CLI binary — and this host renders no terminal statusline, so
    # the file is never written. limitgate, the park, and the reset ring then all fail open at once.
    #
    # Failing open is right (absence of signal is not evidence of headroom). Failing open in SILENCE
    # is the defect: nothing could tell "plenty of headroom" from "this gate cannot arm".
    print("usage-limit inertness is announced, not silent:")
    lp = make_sandbox()
    try:
        _limits = os.path.join(lp, ".game_loop", "limits.json")
        _bin = os.path.join(lp, ".game_loop", "bin", "game_loop")

        def _status(entrypoint="cli"):
            return subprocess.run([_bin, "status"], capture_output=True, text=True,
                                  env=_env(lp, sid="sess-lim",
                                           CLAUDE_CODE_ENTRYPOINT=entrypoint)).stdout

        # The keystone: the tap writes the snapshot even when the payload carries NO windows. That
        # is what makes "no file" mean "never ran" rather than "ran and had nothing to say" — the
        # inference the whole diagnosis rests on, so it is asserted rather than assumed.
        if os.path.exists(_limits):
            os.remove(_limits)
        subprocess.run([_bin, "statusline"], input="{}", capture_output=True, text=True,
                       env=_env(lp, sid="sess-lim"))
        check("the tap writes a snapshot even for an EMPTY payload — so a missing file proves the "
              "tap never ran",
              os.path.isfile(_limits))
        os.remove(_limits)

        _none = _status()
        check("no snapshot → the inertness is stated, not left as a missing file",
              "USAGE-LIMIT PROTECTION IS INERT" in _none)
        check("...and it names all three gates that are off, not just the file",
              "no page at the threshold" in _none and "no park at exhaustion" in _none
              and "no ring when the" in _none)
        check("...and it no longer reads as a benign not-yet",
              "fills on the first API response" not in _none)
        _vs = _status("claude-vscode")
        check("under the VSCode extension it says UNAVAILABLE rather than pending",
              "UNAVAILABLE here rather than pending" in _vs and "no terminal statusline" in _vs)
        check("...and names the host where the tap does fire, since that is the only remedy",
              "`claude` in a shell" in _vs)
        check("under any other host the remedy is the wiring, not the host",
              "statusLine` is registered" in _none and "UNAVAILABLE here" not in _none)

        # A snapshot that exists but carries no windows is JUST AS INERT. For the human waiting on
        # a page the two are the same event, so they must not report differently.
        with open(_limits, "w") as f:
            json.dump({"captured_at": time.time(), "windows": {}}, f)
        _empty = _status()
        check("a snapshot with no windows is inert too, and says which cause it is",
              "USAGE-LIMIT PROTECTION IS INERT" in _empty and "API-key auth" in _empty)

        # PAIRED NEGATIVE (INV8): without this, a build that warned unconditionally would pass every
        # assertion above. The warning must be ABSENT exactly when the gates can actually arm.
        with open(_limits, "w") as f:
            json.dump({"captured_at": time.time(), "windows": {"five_hour": {
                "used_percentage": 12.0, "resets_at": time.time() + 3600,
                "crossed_at": None, "notified": False}}}, f)
        _live = _status()
        check("a snapshot carrying a real window silences the warning entirely",
              "USAGE-LIMIT PROTECTION IS INERT" not in _live)
        check("...and the ordinary limits row is what appears instead",
              "limits: " in _live and "5h" in _live)
    finally:
        shutil.rmtree(lp, ignore_errors=True)

    # THE TAP'S OWN FAILURE, which was the other half of the same silence. The registered command
    # resolves its own path; the old form discarded stdin and printed nothing when it could not, so
    # a mis-resolved path exited 0 and wrote no snapshot — identical, from outside, to a healthy tap
    # feeding healthy gates. A statusline is the one surface guaranteed to be read where one is
    # rendered, so that is where it now says what is wrong.
    print("the statusline tap announces its own absence:")
    import re as _re
    _isrc = open(os.path.join(REPO, "install.sh")).read()
    _m = _re.search(r"GL_STATUSLINE = \((.*?)\)\n", _isrc, _re.S)
    _cmd = "".join(_re.findall(r"'([^']*)'", _m.group(1)))
    with open(os.path.join(REPO, ".claude", "settings.json")) as f:
        _wired = json.load(f)["statusLine"]["command"]
    check("the wired statusLine is the SAME string install.sh ships — so the two cannot drift",
          _wired == _cmd)
    check("...and it dispatches through the pin and sets GAME_LOOP_HOME, exactly as the hooks do",
          ".game_loop_self" in _cmd and "GAME_LOOP_HOME=" in _cmd)

    _tp = make_sandbox()
    try:
        def _tap(root):
            return subprocess.run(["bash", "-c", _cmd], input="{}", capture_output=True, text=True,
                                  env=_env(root))

        _ok = _tap(_tp)
        _snap = os.path.join(_tp, ".game_loop", "limits.json")
        check("with the tap present it RUNS — the snapshot appears and no complaint is printed",
              os.path.isfile(_snap) and "tap not found" not in _ok.stdout)

        # PAIRED (INV8): move the tap out of reach. Without this arm, the assertion above passes
        # against a command that can only ever succeed, and the swallow would be invisible again.
        os.remove(_snap)
        os.rename(os.path.join(_tp, ".game_loop", "bin", "game_loop"),
                  os.path.join(_tp, ".game_loop", "bin", "game_loop.moved"))
        _gone = _tap(_tp)
        check("with the tap unreachable it SAYS SO rather than exiting 0 in silence",
              "tap not found" in _gone.stdout and "inert" in _gone.stdout)
        check("...and it still exits 0 and writes nothing — a statusline must never break the UI",
              _gone.returncode == 0 and not os.path.exists(_snap))
    finally:
        shutil.rmtree(_tp, ignore_errors=True)

    # AN UNREADABLE COMMIT TARGET FAILS CLOSED (#40). The blind spot was documented; its blast
    # radius under fan-out was not. A variable resolves to nothing the scan can match against the
    # repo, so the commit read as "targets some other project" and passed UNEXAMINED — and an
    # orchestrator reaches every worktree through a variable, which makes the gate escaped by
    # DEFAULT there rather than occasionally, with no output telling "checked and fine" apart from
    # "never looked".
    #
    # The narrowness is the hard part, and it is empirical rather than tidy: failing closed on any
    # unparseable fragment was tried in the field and refused two legitimate messages in one
    # afternoon, both of them BUG REPORTS about this guard. Prose about a commit is not a commit.
    # WAITING ON DISPATCHED WORK (#32). Idleness is transcript growth, and a subagent's output does
    # not grow the parent's transcript — so a run that has correctly handed out every piece of work
    # and is waiting for results it cannot hurry is byte-for-byte identical to one asleep. The ring
    # is cheap; the ESCALATION pages the human, which is a T3 spend on a run behaving perfectly.
    #
    # The argument needs no incident: the cap counts CONSECUTIVE UNPRODUCTIVE rings, so the only
    # protection today is the agent finding filler while it waits. A safety property that degrades
    # as the agent gets more disciplined is broken in a way that looks like working.
    print("a declared wait holds the ring cap, and cannot become an off switch (#32):")
    wp = make_sandbox()
    try:
        _wcfg = os.path.join(wp, ".game_loop", "config.json")
        _wsd = os.path.join(wp, ".game_loop", "sessions", "sess-wait")
        os.makedirs(_wsd, exist_ok=True)
        _wlog = os.path.join(wp, ".game_loop", "log.jsonl")

        def _probe(cmd):
            with open(_wcfg) as f:
                c = json.load(f)
            c["watchdog"] = dict(c.get("watchdog") or {}, waiting_probe=cmd)
            with open(_wcfg, "w") as f:
                json.dump(c, f)

        def _at_cap():
            """A session already at the ring cap, mandate bound, nothing else to explain the idle."""
            with open(os.path.join(_wsd, "state.json"), "w") as f:
                json.dump({"mandate": {"active": True, "text": "keep going", "at": "now"},
                           "watchdog_rings": 99, "watchdog_last_ring_size": 10 ** 9}, f)

        _tp = os.path.join(wp, "transcript.jsonl")
        with open(_tp, "w") as f:
            f.write("{}\n")

        def _run():
            return run_watchdog(os.path.join(wp, ".game_loop", "bin", "watchdog"),
                                {"session_id": "sess-wait", "transcript_path": _tp},
                                WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0")

        # PAIRED FIRST, so "held" means something: with no probe the cap escalates as it always has.
        _at_cap()
        _run()
        with open(_wlog) as f:
            check("with no probe configured the cap still stands the watchdog down — unchanged",
                  '"watchdog_exhausted"' in f.read())

        open(_wlog, "w").close()
        _probe("echo 'two crawlers still live'; exit 0")
        _at_cap()
        _run()
        with open(_wlog) as f:
            _log = f.read()
        # A VERIFIED WAIT STOPS THE RING, not just the escalation. Guarding only the cap left the
        # run being WOKEN every idle_sec to rediscover it had nothing to do — a turn burnt each
        # time. Measured on this repo before fixing: the queue emptied, the probe said "waiting",
        # and the watchdog rang on a loop anyway.
        open(_wlog, "w").close()
        _probe("echo nothing to do; exit 0")
        with open(os.path.join(_wsd, "state.json"), "w") as f:
            json.dump({"mandate": {"active": True, "text": "keep going", "at": "now"},
                       "watchdog_rings": 0, "watchdog_last_ring_size": 0}, f)
        # THE ENVIRONMENT CONTRACT. Triggers and the waiting probe both run a PROJECT-supplied
        # command, and they were handing it different environments — so a probe written to the
        # documented trigger contract expanded $GAME_LOOP_ROOT to nothing, failed, and read as NOT
        # WAITING. The conservative default hid it: the watchdog rang exactly the run the probe
        # existed to leave alone, and nothing looked broken.
        _probe('test -n "$GAME_LOOP_ROOT" && test -n "$GAME_LOOP_REPO" && echo env-ok')
        with open(os.path.join(_wsd, "state.json"), "w") as f:
            json.dump({"mandate": {"active": True, "text": "keep going", "at": "now"},
                       "watchdog_rings": 0, "watchdog_last_ring_size": 0}, f)
        _env_run = _run()
        with open(_wlog) as f:
            check("the waiting probe gets the SAME environment a trigger gets — two runners of "
                  "project-supplied commands must not differ in what they provide",
                  _env_run.returncode == 0 and "env-ok" in f.read())

        # ...AND IT KNOWS WHICH SESSION IT SPEAKS FOR (#60). Reported from a checkout running 18
        # concurrent sessions: the natural liveness signal is the mtime of per-session artifacts,
        # and that path is keyed by session. With no id, a probe can only glob across every session
        # sharing the checkout, so it answers WAITING because SOMEBODY ELSE's work is live. That is
        # a false waiting — the one direction that fails silent — and it silences the watchdog on
        # exactly the wedged run it exists to catch. The reporter declined to wire a probe at all
        # rather than install that, which means the feature was unusable for its own motivating case.
        open(_wlog, "w").close()
        _probe('test "$GAME_LOOP_SESSION" = "sess-wait" '
               '&& test -d "$GAME_LOOP_SESSION_DIR" && echo sid-ok')
        with open(os.path.join(_wsd, "state.json"), "w") as f:
            json.dump({"mandate": {"active": True, "text": "keep going", "at": "now"},
                       "watchdog_rings": 0, "watchdog_last_ring_size": 0}, f)
        _sid_run = _run()
        with open(_wlog) as f:
            check("...and it is told its own SESSION, and a session dir that exists — so a probe in "
                  "a checkout shared by many sessions can scope itself instead of globbing across "
                  "everyone else's live work",
                  _sid_run.returncode == 0 and "sid-ok" in f.read())

        # PAIRED, on the function that decides it, because `_run` always supplies a session and an
        # assertion through it could never see the other case. What matters is that an UNRESOLVED
        # session is handed over as empty rather than as a plausible wrong id: a probe told it is
        # session "" can decline; a probe told it is session "None" will confidently scope itself
        # to a directory that does not exist and report whatever that emptiness implies.
        _wd_ld = importlib.machinery.SourceFileLoader(
            "wd_sid", os.path.join(wp, ".game_loop", "bin", "watchdog"))
        _wd = importlib.util.module_from_spec(importlib.util.spec_from_loader("wd_sid", _wd_ld))
        _wd_ld.exec_module(_wd)
        _wd.set_session("sess-wait")
        _with = _wd.session_env()
        _wd.set_session(None)
        _without = _wd.session_env()
        check("...while a run with NO session resolved hands the probe an EMPTY id and an empty "
              "dir, never a plausible wrong one — 'I cannot scope myself' has to stay answerable",
              _with["GAME_LOOP_SESSION"] == "sess-wait"
              and _with["GAME_LOOP_SESSION_DIR"].endswith(os.path.join("sessions", "sess-wait"))
              and _without["GAME_LOOP_SESSION"] == ""
              and _without["GAME_LOOP_SESSION_DIR"] == "")
        open(_wlog, "w").close()
        _probe("echo nothing to do; exit 0")
        with open(os.path.join(_wsd, "state.json"), "w") as f:
            json.dump({"mandate": {"active": True, "text": "keep going", "at": "now"},
                       "watchdog_rings": 0, "watchdog_last_ring_size": 0}, f)
        _r0 = _run()
        check("a verified wait stops the RING itself — a run with nothing to do is not woken to "
              "discover that again",
              _r0.returncode == 0)
        with open(_wlog) as f:
            check("...and the quiet says WHY, so a watchdog that has stood down is distinguishable "
                  "from one that is broken",
                  "not ringing a run" in f.read())
        # PAIRED: with the probe saying NOT waiting, the same idle state still rings.
        open(_wlog, "w").close()
        _probe("exit 1")
        with open(os.path.join(_wsd, "state.json"), "w") as f:
            json.dump({"mandate": {"active": True, "text": "keep going", "at": "now"},
                       "watchdog_rings": 0, "watchdog_last_ring_size": 0}, f)
        check("...while the same idle run DOES ring when nothing declares a wait — the quiet is the "
              "probe's doing, not the watchdog having stopped working",
              _run().returncode == 2)

        check("a verifying wait HOLDS the cap — the human is not paged about a healthy run",
              '"watchdog_wait_held"' in _log and '"watchdog_exhausted"' not in _log)
        check("...and the verdict is logged with its detail, so 'how often did this silence the "
              "watchdog' is a fact rather than a hunch",
              '"watchdog_waiting"' in _log and "two crawlers still live" in _log)

        # THE OFF-SWITCH CONTROLS. Each of these must still escalate, or the verb has handed the
        # agent's own judgement back to it — which is the thing the watchdog exists not to trust.
        for _name, _cmd in (("a probe that says NOT waiting", "exit 1"),
                            ("a probe that cannot run at all", "/nonexistent/probe/xyz"),
                            ("a probe that hangs past its bound", "sleep 60")):
            open(_wlog, "w").close()
            _probe(_cmd)
            _at_cap()
            _run()
            with open(_wlog) as f:
                _l = f.read()
            check(f"...{_name} escalates — conservative toward NOT waiting, since a false 'waiting' "
                  "silences the watchdog on exactly the wedged run it was built to catch",
                  '"watchdog_exhausted"' in _l and '"watchdog_wait_held"' not in _l)

        # "COULD NOT TELL" IS A THIRD STATE, and it used to be spent as "not waiting".
        #
        # Conservative-toward-not-waiting is right and it is not self-reporting: a broken probe and
        # a working one seeing work produce the same non-zero exit, the same ring, and no way to
        # tell them apart. Measured, not imagined -- this repo's own probe bailed on `command -v gh`
        # because gh is absent from a hook's PATH, exited 1 with EMPTY stderr, and read as "there is
        # work" for five rounds while I reported the loop fixed. The contract is now 0 or 1; every
        # other code means the probe could not answer, and SAYS so.
        for _name, _cmd in (("an unexpected exit code", "exit 3"),
                            ("a probe that cannot run at all", "/nonexistent/probe/xyz"),
                            ("a probe that hangs past its bound", "sleep 60")):
            open(_wlog, "w").close()
            _probe(_cmd)
            _at_cap()
            _run()
            with open(_wlog) as f:
                _l = f.read()
            check(f"...{_name} is recorded as UNANSWERED, not as 'not waiting' — a check that "
                  "cannot run must not borrow the credibility of one that ran and said no",
                  '"answered": false' in _l)
            check(f"...and status says the probe is FAILING for {_name}, so configured-but-inert "
                  "is loud instead of indistinguishable from a busy queue",
                  "THE WAITING PROBE IS FAILING" in gl(wp, "status", sid="sess-wait").stdout)

        # PAIRED: a probe that genuinely answers "there is work" must NOT be smeared as broken, or
        # the warning becomes noise and stops carrying information.
        open(_wlog, "w").close()
        _probe("exit 1")
        _at_cap()
        _run()
        with open(_wlog) as f:
            check("...while a probe that answers 'there is work' is recorded as ANSWERED — the "
                  "warning marks breakage only, so it still means something when it fires",
                  '"answered": true' in f.read())
        _st_ans = gl(wp, "status", sid="sess-wait").stdout
        check("...and status reports it as an ordinary 'not waiting', not as a failure",
              "not waiting" in _st_ans and "THE WAITING PROBE IS FAILING" not in _st_ans)

        # And the shipped probe honours the contract it is judged by. A hook's PATH is not a login
        # shell's, which is the exact way this failed: resolving `gh` by name alone.
        _shipped = os.path.join(SRC_GAME_LOOP, "triggers.d", "waiting-on-issues.sh")
        if os.path.exists(_shipped):
            _p = subprocess.run(["env", "-i", "PATH=/usr/bin:/bin",
                                 "HOME=" + os.path.expanduser("~"), "bash", _shipped],
                                capture_output=True, text=True, timeout=60)
            # The assertion is about PATH RESOLUTION, not about the tracker being reachable. An
            # unauthenticated or offline gh is a real "could not tell" and exits 2 honestly; failing
            # to FIND gh under a hook's PATH is the defect that cost five rounds, and it is the one
            # thing this environment can decide. Asserting `returncode in (0, 1)` instead would tie
            # the suite to network and credentials, which is a different test wearing this one's name.
            check("this repo's own waiting probe RESOLVES its tracker CLI under a minimal PATH — "
                  "the environment a hook actually gets, not the one a hand-check is run in",
                  "not found on PATH" not in _p.stderr)

        # CONFIG-AUTHORED ONLY. A wait the agent can declare for itself is the off switch.
        _bin_src = open(os.path.join(SRC_GAME_LOOP, "bin", "game_loop")).read()
        check("no verb can set the probe — it is config-authored only, never runtime-settable",
              "waiting_probe" not in _bin_src.split("def main(")[-1])
        _probe("echo held; exit 0")
        _at_cap()
        _run()
        check("status reports what the run is blocked on — the first question a resume asks",
              "WAITING on dispatched work" in gl(wp, "status", sid="sess-wait").stdout)
        # The other three arms of the same report, since one assertion covering a producer is how a
        # producer ends up unprotected while looking covered.
        _probe("exit 1")
        _at_cap()
        _run()
        check("...and says NOT WAITING when that was the last verdict, rather than going quiet — "
              "silence would read as 'no probe configured', which is a different fact",
              "not waiting" in gl(wp, "status", sid="sess-wait").stdout)
        open(_wlog, "w").close()
        check("...and with a probe configured but never yet run, says exactly that",
              "has not run it yet" in gl(wp, "status", sid="sess-wait").stdout)
        # PAIRED: no probe, no line at all. Most installs fan nothing out and want none of this.
        with open(_wcfg) as f:
            _c = json.load(f)
        _c["watchdog"] = {k: v for k, v in (_c.get("watchdog") or {}).items()
                          if k != "waiting_probe"}
        with open(_wcfg, "w") as f:
            json.dump(_c, f)
        check("...and with no probe configured it is silent — the feature announces nothing to the "
              "installs that never asked for it",
              "waiting:" not in gl(wp, "status", sid="sess-wait").stdout)
    finally:
        shutil.rmtree(wp, ignore_errors=True)

    # A DEPLOY VERB NEEDS A BOUNDARY ON BOTH SIDES (#51). There was one before the verb and none
    # after, so it matched as a SUBSTRING and ordinary English refused the call: "file surgery"
    # contains " surge"; "docker pushed" contains the default verb. The refusal is loud and
    # correct-sounding and names a verb nobody typed, so the natural response is to rephrase and
    # move on — never learning the guard was wrong. That is how a guard trains people around it.
    # THE ENTRY POINT RUNS ITSELF (#48). A tree could carry a perfectly provisioned harness and
    # still run completely bare: the PreToolUse guards fire without being asked, but everything
    # behind a VERB — the claim gate, harden, the mandate machinery, the retro, `status` itself —
    # only exists for an agent that goes looking. A banner saying "run status" would have been rung
    # 6 of the ladder applied to the gap that decides whether any other rung runs (INV1), so this
    # runs the verb rather than advertising it.
    # THE STAMP MUST DESCRIBE WHAT LANDED. install.sh copies the payload from the source WORKING
    # TREE and stamps the sha from HEAD — the same thing only when that tree is clean. Installing
    # from a checkout with uncommitted work recorded a commit whose content is not what was copied,
    # and `status` uses that stamp to decide whether a re-install is due, so the target reported
    # itself current while carrying files that exist in no commit.
    #
    # NOT REFUSED, MARKED: installing from a work-in-progress checkout is legitimate — it is how
    # this repo is developed. Recording it as though it were a commit is not.
    # #45 — NEVER BE FAR FROM THE EDGE. The limit gate's purpose was never "know the number"; it
    # was "do not die with your state only in your head". Warning was one way to buy that, and it is
    # the way that depends on a number this host cannot have — rate_limits rides the statusline
    # payload and nothing else. So the handoff is REFRESHED at every turn-end instead: a limit death
    # then costs at most one turn, in any host, with no configured budget standing for nothing.
    print("the handoff is maintained, not demanded at the cliff (#45):")
    hf = make_sandbox()
    try:
        _hsid = "sess-handoff"
        _hp = os.path.join(hf, ".game_loop", "sessions", _hsid, "HANDOFF.md")
        _tp = os.path.join(hf, "transcript.jsonl")
        with open(_tp, "w") as f:
            f.write("{}\n")

        def _turn_end():
            return subprocess.run([os.path.join(hf, ".game_loop", "bin", "game_loop"), "stopgate"],
                                  input=json.dumps({"session_id": _hsid, "transcript_path": _tp}),
                                  capture_output=True, text=True, env=_env(hf, sid=_hsid))

        gl(hf, "mandate", "--set", "land the parser", sid=_hsid)
        gl(hf, "checkpoint", "--notes", "parser lexes; the emitter is next", sid=_hsid)
        _turn_end()
        check("a turn-end leaves a usable handoff on disk without anyone asking for one",
              os.path.isfile(_hp) and os.path.getsize(_hp) > 0)
        _h = ""
        if os.path.isfile(_hp):
            with open(_hp) as f:
                _h = f.read()
        check("...and it carries the job and the last reported progress — the two things a dead "
              "run's successor has to re-derive",
              "land the parser" in _h and "the emitter is next" in _h)

        # THE TRAP THIS CLOSES. The limit gate is satisfied by a fresh handoff, so a file the
        # harness writes every turn would make that gate pass forever without the agent doing
        # anything — a check turned vacuous by the safety net added to help it.
        with open(os.path.join(hf, ".game_loop", "config.json")) as f:
            _hc = json.load(f)
        _hc["limits"] = dict(_hc.get("limits") or {}, threshold_pct=1)
        with open(os.path.join(hf, ".game_loop", "config.json"), "w") as f:
            json.dump(_hc, f)
        with open(os.path.join(hf, ".game_loop", "limits.json"), "w") as f:
            json.dump({"captured_at": time.time(), "windows": {"five_hour": {
                "used_percentage": 99.0, "resets_at": time.time() + 3600,
                "crossed_at": time.time() - 60, "notified": True}}}, f)
        _turn_end()                                    # refresh the AUTO handoff after the crossing
        _gate = subprocess.run([os.path.join(hf, ".game_loop", "bin", "game_loop"), "limitgate"],
                               input=json.dumps({"session_id": _hsid, "tool_name": "Bash",
                                                 "tool_input": {"command": "echo hi"}}),
                               capture_output=True, text=True, env=_env(hf, sid=_hsid))
        check("the AUTO handoff does NOT satisfy the limit gate — the generated file is the floor, "
              "the agent's own account is still owed",
              '"deny"' in _gate.stdout.replace(" ", ""))
        # PAIRED: a handoff the agent actually wrote DOES open it, or the gate is unsatisfiable.
        with open(_hp, "w") as f:
            f.write("where I got to, in my own words\n")
        _gate2 = subprocess.run([os.path.join(hf, ".game_loop", "bin", "game_loop"), "limitgate"],
                                input=json.dumps({"session_id": _hsid, "tool_name": "Bash",
                                                  "tool_input": {"command": "echo hi"}}),
                                capture_output=True, text=True, env=_env(hf, sid=_hsid))
        check("...while a handoff somebody actually WROTE opens it, so the gate stays satisfiable",
              '"deny"' not in _gate2.stdout.replace(" ", ""))
        _turn_end()
        check("...and a hand-written handoff is never overwritten by the next turn-end",
              os.path.isfile(_hp)
              and open(_hp).read().strip() == "where I got to, in my own words")

        # THE EVIDENCE HALF. Recorded, gating nothing — INV4 applied to ourselves, since a threshold
        # invented today would be a number standing for nothing.
        _tp2 = os.path.join(hf, "t2.jsonl")
        _now = datetime.datetime.now(datetime.timezone.utc)
        _old = _now - datetime.timedelta(hours=9)
        with open(_tp2, "w") as f:
            for _when, _tok in ((_now, 100), (_now, 50), (_old, 999999)):
                f.write(json.dumps({"timestamp": _when.isoformat().replace("+00:00", "Z"),
                                    "message": {"usage": {"output_tokens": _tok}}}) + "\n")
        subprocess.run([os.path.join(hf, ".game_loop", "bin", "game_loop"), "stopgate"],
                       input=json.dumps({"session_id": _hsid, "transcript_path": _tp2}),
                       capture_output=True, text=True, env=_env(hf, sid=_hsid))
        with open(os.path.join(hf, ".game_loop", "log.jsonl")) as f:
            _uw = [json.loads(l) for l in f if '"usage_window"' in l]
        check("the trailing window's consumption is RECORDED as evidence",
              bool(_uw) and _uw[-1]["output_tokens"] == 150 and _uw[-1]["records"] == 2)
        check("...and the window actually windows — a record outside it is excluded, so this is a "
              "measurement rather than a running total",
              bool(_uw) and _uw[-1]["output_tokens"] == 150)
        # It must gate NOTHING. A threshold today would be invented, which is the defect the landed
        # half of #45 fixed; this is the evidence a real one would need first.
        with open(os.path.join(SRC_GAME_LOOP, "bin", "game_loop")) as f:
            _gsrc = f.read()
        check("...and nothing compares it against a threshold: no gate without a logged, observed "
              "failure, and there is not one yet",
              "usage_window" in _gsrc
              and "output_tokens" not in _gsrc.split("def _limitgate_verdict")[1][:3000])
    finally:
        shutil.rmtree(hf, ignore_errors=True)

    print("an install records what it actually installed:")
    ds = make_sandbox()
    try:
        _dv = os.path.join(ds, ".game_loop", "VERSION")

        def _notice(stamp):
            with open(_dv, "w") as f:
                f.write(stamp + "\n")
            return gl(ds, "status", sid="sess-dirty").stdout

        _d = _notice("a" * 40 + "-dirty")
        check("a -dirty stamp is reported as an install whose sha does not describe its files",
              "INSTALLED FROM AN UNCOMMITTED TREE" in _d)
        check("...and it says no update check can be meaningful against it, rather than computing "
              "an answer about a commit that was never installed",
              "no update check can be meaningful" in _d)
        # PAIRED: a clean stamp must not trip it, or the warning is a permanent banner.
        check("...while an ordinary clean stamp says nothing of the kind",
              "INSTALLED FROM AN UNCOMMITTED TREE" not in _notice("b" * 40))

        # The installer's half, asserted at the source: it must consult the source tree's state at
        # all. Without this, the marking above is a reader for a mark nobody writes.
        with open(os.path.join(REPO, "install.sh")) as f:
            _isrc = f.read()
        check("install.sh asks whether the SOURCE checkout is dirty before stamping",
              'git -C "$SRC" diff --quiet HEAD' in _isrc and '-dirty' in _isrc)
        check("...and it MARKS rather than refuses — installing from a work-in-progress checkout is "
              "how this repo is developed",
              "exit 1" not in _isrc.split('git -C "$SRC" diff --quiet HEAD')[1][:400])

        # GIT RESOLVES BY WALKING UP. A vendored source — one with no .git of its own, which is what
        # any extraction or package manager produces — makes `git -C "$SRC" rev-parse HEAD` succeed
        # against the CONSUMING repo and answer with that repo's HEAD. Measured before fixing: a
        # game_loop archive inside a throwaway consumer reported the consumer's sha. The failure is
        # silent in the worst way — a plausible sha, from a real repository, describing none of the
        # installed files.
        check("install.sh asks WHOSE checkout the source is, rather than trusting that git answered "
              "about it",
              'rev-parse --show-toplevel' in _isrc and "SRC_OWN" in _isrc)
        check("...and the network fallback is gated on having FETCHED the payload — asking GitHub "
              "for a ref's sha describes a download, and describes nothing about a directory "
              "somebody handed us",
              'FETCHED:-0' in _isrc)

        # End to end, because the structural checks above would pass against a variable nobody reads.
        _van = tempfile.mkdtemp(prefix="gl-vendor-")
        try:
            def _g(cwd, *a):
                return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                      cwd=cwd, capture_output=True, text=True)

            _g(_van, "init", "-q", ".")
            _g(_van, "commit", "-q", "--allow-empty", "-m", "consumer")
            _consumer_sha = _g(_van, "rev-parse", "HEAD").stdout.strip()
            _vend = os.path.join(_van, "vendored")
            os.makedirs(_vend)
            # COPIED, not `git archive`d. The suite must run in a tree that is NOT a git checkout —
            # which is exactly the shape a packager gates, an extraction of a blessed commit. With
            # `git archive` the fixture silently produced nothing there and the next open() died,
            # so the suite was unrunnable in the distribution shape and read as "this commit fails
            # its own gate". Found by a downstream packager doing precisely that, and it is the same
            # assumption this whole block is about: asking git a question about a tree it does not
            # own. Copying needs no .git and tests the working copy, which is what is wanted anyway.
            shutil.copytree(os.path.join(REPO, ".game_loop"), os.path.join(_vend, ".game_loop"),
                            ignore=shutil.ignore_patterns("sessions", "probe", "*.pid", "log.jsonl",
                                                          "triggers.d", "triggers.json"))
            shutil.copy(os.path.join(REPO, "install.sh"), os.path.join(_vend, "install.sh"))
            os.makedirs(os.path.join(_vend, "templates"), exist_ok=True)
            shutil.copy(os.path.join(REPO, "templates", "settings.hooks.json"),
                        os.path.join(_vend, "templates", "settings.hooks.json"))
            shutil.copy(os.path.join(REPO, "templates", "verify.yaml"),
                        os.path.join(_vend, "templates", "verify.yaml"))
            os.chmod(os.path.join(_vend, "install.sh"), 0o755)
            # VERSION is UNTRACKED, so `git archive` never carries it — a vendored tree starts with
            # no stamp at all, which is precisely the case that used to reach for the consumer's
            # HEAD. Removed defensively rather than assumed absent.
            _vv = os.path.join(_vend, ".game_loop", "VERSION")
            if os.path.exists(_vv):
                os.remove(_vv)
            _tgt = os.path.join(_van, "target")
            os.makedirs(_tgt)
            _g(_tgt, "init", "-q", ".")
            _r = subprocess.run([os.path.join(_vend, "install.sh"), _tgt],
                                capture_output=True, text=True, env=_env(_tgt))
            _stamp = os.path.join(_tgt, ".game_loop", "VERSION")
            check("a VENDORED source stamps NOTHING rather than the consuming repo's HEAD — an "
                  "absent stamp is honest, a wrong one makes the update check confidently wrong",
                  not os.path.exists(_stamp) and "no VERSION stamped" in _r.stdout)
            check("...and the consumer's own sha appears nowhere in what was written",
                  _consumer_sha and _consumer_sha not in _r.stdout)
            # PAIRED: a packager that DID record the commit has its record carried forward.
            os.makedirs(os.path.join(_vend, ".game_loop"), exist_ok=True)
            with open(os.path.join(_vend, ".game_loop", "VERSION"), "w") as f:
                f.write("c" * 40 + "\n")
            _tgt2 = os.path.join(_van, "target2")
            os.makedirs(_tgt2)
            _g(_tgt2, "init", "-q", ".")
            subprocess.run([os.path.join(_vend, "install.sh"), _tgt2],
                           capture_output=True, text=True, env=_env(_tgt2))
            with open(os.path.join(_tgt2, ".game_loop", "VERSION")) as f:
                check("...while a vendored source that DOES carry a recorded VERSION has it carried "
                      "forward — the packager is the only party that knows which commit it extracted",
                      f.read().strip() == "c" * 40)
            # THE LEVEL HAS THE SAME TRANSPORT GAP, and it is the worse half: an extraction has no
            # tags, so the resolver correctly finds no mark and correctly applies the default —
            # meaning a commit tagged stable installs itself as ALPHA, silently, because alpha IS
            # the default. Measured end to end by a packager an hour after the scheme shipped.
            with open(os.path.join(_vend, ".game_loop", "CONFIDENCE"), "w") as f:
                f.write("stable\n")
            _tgt3 = os.path.join(_van, "target3")
            os.makedirs(_tgt3)
            _g(_tgt3, "init", "-q", ".")
            subprocess.run([os.path.join(_vend, "install.sh"), _tgt3],
                           capture_output=True, text=True, env=_env(_tgt3))
            with open(os.path.join(_tgt3, ".game_loop", "CONFIDENCE")) as f:
                check("a vendored source carrying a recorded LEVEL has it honoured — the same file "
                      "this installer writes, so no package manager is named on either side",
                      f.read().strip() == "stable")
            # PAIRED: a level nobody recorded, or one that is not a level, must fall back to alpha
            # rather than to whatever the file happened to contain.
            with open(os.path.join(_vend, ".game_loop", "CONFIDENCE"), "w") as f:
                f.write("totally-blessed\n")
            _tgt4 = os.path.join(_van, "target4")
            os.makedirs(_tgt4)
            _g(_tgt4, "init", "-q", ".")
            subprocess.run([os.path.join(_vend, "install.sh"), _tgt4],
                           capture_output=True, text=True, env=_env(_tgt4))
            with open(os.path.join(_tgt4, ".game_loop", "CONFIDENCE")) as f:
                check("...while a value that is not one of the three levels falls back to alpha, so "
                      "a packager cannot invent a level by writing one",
                      f.read().strip() == "alpha")
        finally:
            shutil.rmtree(_van, ignore_errors=True)
    finally:
        shutil.rmtree(ds, ignore_errors=True)

    # CONFIDENCE. A consumer cloning this repo gets whatever was on main that morning, mid-refactor
    # included, and nothing distinguishes "half-landed feature" from "the commit I run my own agent
    # on". A self-declared level would be prose — the thing this project refuses everywhere else —
    # so each level is an artifact the marker cannot fabricate.
    print("a confidence level is an artifact, not a claim:")
    cd = make_sandbox()
    try:
        def _cg(*a):
            return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                  cwd=cd, capture_output=True, text=True)

        def _conf(*a):
            return subprocess.run([os.path.join(cd, ".game_loop", "bin", "game_loop"),
                                   "confidence", *a], cwd=cd, capture_output=True, text=True,
                                  env=_env(cd, sid="sess-conf"))

        _cg("init", "-q", ".")
        with open(os.path.join(cd, "f.txt"), "w") as f:
            f.write("x\n")
        # Ignore generated bytecode BEFORE the first add: running the binary writes __pycache__, and
        # a fixture that commits it makes every later tree "dirty" for reasons that are not work.
        # The product is right to refuse any dirt; the fixture was manufacturing it.
        with open(os.path.join(cd, ".gitignore"), "w") as f:
            # .game_loop_self/ too: a real install gitignores it (install.sh writes that), and the
            # pin directory created below would otherwise make the tree dirty for no work at all.
            # The same runtime paths a real install ignores (install.sh writes these into
            # .game_loop/.gitignore). Without them the fixture manufactures dirt — marking beta
            # writes log.jsonl, which then makes the tree unclean for the NEXT mark. Three fixture
            # corrections in a row here, and every one of them was the product correctly refusing.
            f.write("__pycache__/\n*.pyc\n.game_loop_self/\n"
                    ".game_loop/log.jsonl\n.game_loop/sessions/\n.game_loop/state.json\n"
                    ".game_loop/verified.json\n.game_loop/probe/\n.game_loop/HANDOFF.md\n"
                    ".game_loop/triggers.json\n.game_loop/triggers.d/\n"
                    ".game_loop/CONFIDENCE\n.game_loop/VERSION\n")
        _cg("add", "-A")
        _cg("commit", "-q", "-m", "one")

        _a = _conf()
        check("an UNMARKED commit reads as ALPHA, and says the absence is the default rather than "
              "a judgement — silence must never read as confidence",
              "CONFIDENCE: ALPHA" in _a.stdout and "MID-FLIGHT" in _a.stdout)
        check("...and it says what NO level would mean, rather than implying correctness",
              "none of these say the code is correct" in _a.stdout
              and "Neither is a promise about your project" in _a.stdout)

        # THE REFUSALS ARE THE WHOLE DESIGN. Each is a way the mark could have been fabricated.
        with open(os.path.join(cd, "f.txt"), "a") as f:
            f.write("uncommitted\n")
        check("a DIRTY tree cannot be marked — a mark describes a commit, not a desk",
              _conf("--mark", "beta").returncode != 0)
        _cg("checkout", "--", "f.txt")
        check("alpha cannot be marked at all: it IS the absence of a mark, so marking it would say "
              "nothing",
              _conf("--mark", "alpha").returncode != 0)
        check("stable is refused while the owning agent is NOT running on this commit — dogfooding "
              "is the evidence, and there is no way to assert it without doing it",
              _conf("--mark", "stable").returncode != 0
              and "pinned" in _conf("--mark", "stable").stderr)

        # THE PERMITTING ARMS. A gate nothing can satisfy is as broken as one nothing can fail.
        _r = _conf("--mark", "beta", "--notes", "the parser landed")
        check("a clean tree with its owed checks run CAN be marked beta",
              _r.returncode == 0 and "MARKED BETA" in _r.stdout)
        _sha = _cg("rev-parse", "HEAD").stdout.strip()[:8]
        check("...and the mark is an annotated git TAG, so it travels with an ordinary clone",
              f"beta-{_sha}" in _cg("tag").stdout)
        _b = _conf()
        check("...and reading it back reports BETA with the evidence, not just a verdict",
              "CONFIDENCE: BETA" in _b.stdout and "tree clean at mark time" in _b.stdout
              and "the parser landed" in _b.stdout)
        check("...and it tells the reader to re-check rather than trust the tag",
              "--recheck" in _b.stdout)

        # STABLE, once the agent really is running on it: the pin is the artifact.
        _pin = os.path.join(cd, ".game_loop_self", ".game_loop")
        os.makedirs(_pin)
        with open(os.path.join(_pin, "VERSION"), "w") as f:
            f.write(_cg("rev-parse", "HEAD").stdout.strip() + "\n")
        _st = _conf("--mark", "stable")
        check("with the harness actually pinned to this commit, stable becomes markable — and says "
              "the dogfooding out loud",
              _st.returncode == 0 and "dogfooded" in _st.stdout)
        check("...and stable outranks beta when both mark the same commit",
              "CONFIDENCE: STABLE" in _conf().stdout)

        # MARKING IS A TRIGGER MOMENT. `confidence --mark` is the exact instant "ready for
        # consumers" stops being an intention and becomes a fact about a sha, so anything that
        # distributes this project belongs HERE rather than in a habit. Left to memory it does not
        # happen: the first stable mark was published by a downstream maintainer who happened to be
        # watching, not by the person who marked it.
        with open(os.path.join(cd, ".game_loop", "triggers.json"), "w") as f:
            json.dump({"confidence": [{"name": "pub", "command": "cat; echo PUBLISHED"}]}, f)
        _cg("commit", "-q", "--allow-empty", "-m", "another")
        _m2 = _conf("--mark", "beta", "--notes", "second")
        check("marking fires a confidence trigger, so distribution happens AT the moment rather "
              "than whenever somebody remembers",
              "PUBLISHED" in _m2.stdout and "triggers · confidence" in _m2.stdout)

        # A BUDGET IS A CAP THAT STARTS OUT TRUE. The publish trigger's 180s met a suite that had
        # reached 183s -- nobody edited the number, the work grew past it, and NOTHING SPOKE at the
        # moment it became impossible. The first signal was a timeout hours later that read as "the
        # publish broke". Same shape as the mutation sweep's denominator, silently short for its
        # entire life. So the margin is reported while it still exists, which is the only moment
        # anybody has the number.
        with open(os.path.join(cd, ".game_loop", "triggers.json"), "w") as f:
            json.dump({"confidence": [{"name": "slow", "command": "cat >/dev/null; sleep 1; echo RAN",
                                       "timeout_sec": 1.2}]}, f)
        _cg("commit", "-q", "--allow-empty", "-m", "near-cap")
        _near = _conf("--mark", "beta", "--notes", "near")
        check("a trigger that SUCCEEDS but eats most of its budget reports the margin — a cap that "
              "says nothing as you approach it is indistinguishable from one you are nowhere near",
              "headroom left" in _near.stdout and "RAN" in _near.stdout)
        check("...and it is reported as a READING, not a failure: the trigger still counts as having "
              "succeeded, or the lesson taught would be to raise the budget to silence the warning",
              "✓ slow" in _near.stdout)
        # PAIRED, or the check is one that always fires: a trigger with room to spare says nothing.
        with open(os.path.join(cd, ".game_loop", "triggers.json"), "w") as f:
            json.dump({"confidence": [{"name": "quick", "command": "cat >/dev/null; echo RAN",
                                       "timeout_sec": 30}]}, f)
        _cg("commit", "-q", "--allow-empty", "-m", "roomy")
        _roomy = _conf("--mark", "beta", "--notes", "roomy")
        check("...while a trigger with real headroom stays silent about it, so the warning means "
              "something when it appears",
              "RAN" in _roomy.stdout and "headroom left" not in _roomy.stdout)
        # RESTORE THE FIXTURE. This sandbox is shared with the retry assertions further down, which
        # look for PUBLISHED — the third time today a mutation of a shared fixture has bled into a
        # later check in this file. Leaving it swapped makes a later assertion fail for a reason that
        # has nothing to do with what it tests.
        with open(os.path.join(cd, ".game_loop", "triggers.json"), "w") as f:
            json.dump({"confidence": [{"name": "pub", "command": "cat; echo PUBLISHED"}]}, f)
        check("...and the payload carries the LEVEL and the sha, so a publisher can decide for "
              "itself what bar it ships at",
              '"level": "beta"' in _m2.stdout and '"tag":' in _m2.stdout)

        # THE CHANNEL POINTER, SO NOBODY DOWNSTREAM ORDERS TAGS (#61). `beta-<sha>` records one
        # mark; `beta` means "the newest thing marked at that level". Ordering annotated tags is a
        # trap — creatordate is right, committerdate returns something much older, both look
        # reasonable, and picking wrong silently pins an older commit that the installer then
        # correctly stamps, so nothing ever contradicts it. Doing it here means it is done once by
        # the only party that already knows the answer.
        def _points_at(ref):
            r = subprocess.run(["git", "rev-parse", ref + "^{commit}"], cwd=cd,
                               capture_output=True, text=True)
            return r.stdout.strip() if r.returncode == 0 else ""

        _head = _points_at("HEAD")
        check("marking moves a CHANNEL pointer beside the immutable tag, so a consumer asks for a "
              "level rather than sorting tags they cannot see",
              _points_at("beta") == _head and _head)
        check("...and the mark tells you to push BOTH — a channel nobody pushed serves the previous "
              "commit, which is a stale answer that looks exactly like a current one",
              "git push origin --force beta" in _m2.stdout)
        # AND IT MUST NOT AIM CONSUMERS OFF THE BRANCH THEY TRACK. Lived, not imagined: another
        # agent pushed to main mid-session, my `git push origin main` was rejected as
        # non-fast-forward, and I pushed the tag and the channel anyway because they were separate
        # commands and the failure was three lines up. For minutes `stable` named a commit missing
        # that agent's work, which my own rebase then orphaned. The fixture has no upstream at all,
        # which is the same "not on the branch consumers track" condition.
        _m_off = _m2      # the same code path, reused rather than re-marked
        check("marking a commit that is not on the tracked branch WARNS before the pointer is "
              "pushed — a channel aimed off-branch names a tree no branch contains",
              "NOT ON YOUR UPSTREAM BRANCH" in _m_off.stdout
              or "push these IN ORDER" in _m_off.stdout)
        check("...and either way the branch push is listed FIRST and the order is called "
              "load-bearing, since these are separate commands and a rejection scrolls away",
              "git push origin HEAD" in _m_off.stdout
              and ("order is load-bearing" in _m_off.stdout
                   or "stop if one is rejected" in _m_off.stdout))

        # PAIRED: it must MOVE on the next mark, or it is an immutable tag wearing a channel's name.
        _cg("commit", "-q", "--allow-empty", "-m", "third")
        _new_head = _points_at("HEAD")
        _m3 = _conf("--mark", "beta", "--notes", "third")
        check("...and the next mark MOVES it — a pointer that stayed put would quietly keep serving "
              "the commit before this one",
              _points_at("beta") == _new_head and _new_head != _head
              and _m3.returncode == 0)

        # A MARK IS ONE-SHOT AND ITS TRIGGERS ARE NOT ITS RECORD. Observed on this repo: the mark
        # landed, its publish trigger declined because HEAD was not pushed YET — correct at that
        # instant — I pushed a minute later, and the re-run died on `tag already exists`. The record
        # existed; the publication it exists to CAUSE did not; and the only recovery was running the
        # trigger script by hand from an absolute path nobody but its author knows. Consumers stayed
        # on the previous stable throughout, which was the build whose fix reads as unfixed.
        check("a first mark says MARKED and not RE-MARKED, so the retry wording is a distinction "
              "the tool draws rather than a phrase it always prints",
              "✓ MARKED BETA" in _m3.stdout and "RE-MARKED" not in _m3.stdout)
        _r1 = _conf("--mark", "beta", "--notes", "third again")
        check("...and marking the SAME commit again is a RETRY, not an error: the immutable record "
              "already exists and is left untouched, and the command no longer dies on its own tag",
              _r1.returncode == 0 and "RE-MARKED BETA" in _r1.stdout)
        check("...and the retry FIRES THE TRIGGERS AGAIN, which is the entire point — a trigger "
              "that declined for a TRANSIENT reason had no way back before this",
              "PUBLISHED" in _r1.stdout)
        check("...and the retry does not move the channel off the commit it already served",
              _points_at("beta") == _new_head)
        # THE ARM THAT MUST STILL DIE. A retry is safe only because the record is unchanged; a tag
        # of that name over a DIFFERENT commit is the immutable record being rewritten, which is the
        # one thing immutability is for.
        _cg("commit", "-q", "--allow-empty", "-m", "fourth")
        _f_sha = _cg("rev-parse", "HEAD").stdout.strip()
        _cg("tag", "-a", "beta-" + _f_sha[:8], "-m", "planted", _new_head)
        _r2 = _conf("--mark", "beta", "--notes", "fourth")
        check("...while that name over a DIFFERENT commit still REFUSES, and names the commit it "
              "already records rather than reporting a bare git error",
              _r2.returncode != 0 and _new_head[:8] in (_r2.stdout + _r2.stderr))

        # THE INSTALLED SIDE. A consumer who took a copy months ago cannot run `confidence` against
        # a clone they no longer have, so the level is recorded at install time and status says it.
        _cf = os.path.join(cd, ".game_loop", "CONFIDENCE")
        with open(_cf, "w") as f:
            f.write("alpha\n")
        _as = gl(cd, "status", sid="sess-conf").stdout
        check("an install from an ALPHA commit SAYS so — the scheme fails entirely if absence reads "
              "as reassurance",
              "INSTALLED FROM AN ALPHA COMMIT" in _as and "mid-flight" in _as)
        with open(_cf, "w") as f:
            f.write("stable\n")
        check("...and a stable install says what stable actually means, not just the word",
              "author's own agent was running on it" in gl(cd, "status", sid="sess-conf").stdout)
        os.remove(_cf)
        check("...while an older install that recorded nothing is silent rather than accused",
              "ALPHA COMMIT" not in gl(cd, "status", sid="sess-conf").stdout)
        with open(os.path.join(REPO, "install.sh")) as f:
            _ish = f.read()
        check("install.sh records the level it installed from, defaulting to alpha",
              "CONFIDENCE" in _ish and 'GL_LEVEL="alpha"' in _ish)
    finally:
        shutil.rmtree(cd, ignore_errors=True)

    print("the session entry point runs itself (#48):")
    ss = make_sandbox()
    try:
        _sscfg = os.path.join(ss, ".game_loop", "config.json")
        _ssprobe = os.path.join(ss, ".game_loop", "probe", "session-start.json")

        def _start(event="SessionStart", sid="sess-ss", source="startup"):
            return subprocess.run([os.path.join(ss, ".game_loop", "bin", "game_loop"),
                                   "sessionstart"],
                                  input=json.dumps({"session_id": sid, "hook_event_name": event,
                                                    "source": source}),
                                  capture_output=True, text=True, env=_env(ss, sid=sid))

        r = _start()
        _out = json.loads(r.stdout)["hookSpecificOutput"]
        check("it returns STATUS as injected context, not a banner telling the agent to run it",
              _out["hookEventName"] == "SessionStart"
              and "INV:" in _out["additionalContext"]
              and "COST LADDER" in _out["additionalContext"])
        check("...and it never blocks: a first impression that bricks a session gets no second one",
              r.returncode == 0)
        check("...and PostCompact is served by the same verb, because compaction is exactly when a "
              "run loses the mandate, the pins and the invariants",
              json.loads(_start(event="PostCompact").stdout)["hookSpecificOutput"]["hookEventName"]
              == "PostCompact")
        check("...and the firing is RECORDED, or this is the statusline tap again — a check whose "
              "silence cannot be told from never having run",
              os.path.isfile(_ssprobe))

        # OPT-OUT, not opt-in: a project that wants silence gets it, but installing the harness is
        # taken as asking for the harness.
        with open(_sscfg) as f:
            _c = json.load(f)
        _c["session_start"] = False
        with open(_sscfg, "w") as f:
            json.dump(_c, f)
        _off = _start()
        check("session_start:false silences it completely — no output, no probe, still exit 0",
              _off.returncode == 0 and not _off.stdout.strip())

        # THE OBSERVABILITY PAIR. A SessionStart hook that stops firing takes the whole entry point
        # with it, and nothing else would notice.
        _c["session_start"] = True
        with open(_sscfg, "w") as f:
            json.dump(_c, f)
        os.remove(_ssprobe)
        _warn = gl(ss, "status", sid="sess-ss").stdout
        check("with no session start ever recorded, status SAYS so",
              "NO SESSION START RECORDED" in _warn)
        check("...and names the remedy, since hooks are read at session start and a warning you "
              "cannot act on is just noise",
              "reload the window" in _warn.lower() and "install.sh" in _warn)
        check("...and states the limit it keeps (INV6): this tracks the PROBE's lifetime, not the "
              "hook's — registered, fired and firing now stay three different claims",
              "PROBE's lifetime" in _warn)
        _start()
        check("...and once one is recorded it goes quiet — the pair that makes the warning mean "
              "something rather than being a permanent banner",
              "NO SESSION START RECORDED" not in gl(ss, "status", sid="sess-ss").stdout)
        _c["session_start"] = False
        with open(_sscfg, "w") as f:
            json.dump(_c, f)
        os.remove(_ssprobe)
        check("...and a project that opted OUT is never nagged about the thing it turned off",
              "NO SESSION START RECORDED" not in gl(ss, "status", sid="sess-ss").stdout)

        # ── THE MOMENT BECAME ATTACHABLE (#63) ────────────────────────────────────────────────
        # Everything above is the moment TELLING a session things. What it could not do is ACT once
        # on the session's behalf, and the only way to get that was a SECOND SessionStart hook
        # registered beside this one: two registrants in one settings file, which makes "registered
        # vs has-fired vs firing-now" harder to diagnose in exchange for no new capability. The
        # instant already existed; it simply was not attachable.
        _mark = os.path.join(ss, "acted.txt")
        _sstj = os.path.join(ss, ".game_loop", "triggers.json")

        def _attach(cfg):
            with open(_sstj, "w") as f:
                json.dump(cfg, f)

        def _ssctx(r):
            """The additionalContext a session start injected, or '' if it said nothing at all."""
            try:
                return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
            except (ValueError, KeyError):
                return ""

        def _ss_enabled(on):
            _c["session_start"] = on
            with open(_sscfg, "w") as f:
                json.dump(_c, f)

        # THE CONTROL FIRST, and it is proved by a FILE rather than by an empty stdout. "No output"
        # is precisely the evidence that cannot tell 'did not run' from 'ran and said nothing', and
        # an opt-out that suppressed the REPORT while still running the command would be the worse
        # half of both. (config is still session_start:false, left that way by the check above.)
        _attach({"session_start": [{"name": "ss-act",
                                    "command": f"tee -a {shlex.quote(_mark)}; echo ACTED-ONCE"}]})
        _offr = _start()
        check("session_start:false stops the ATTACHMENT too, and the marker file it would have "
              "written never appears — the opt-out reaches the acting half, not just the reporting "
              "half",
              _offr.returncode == 0 and not _offr.stdout.strip() and not os.path.exists(_mark))
        _ss_enabled(True)
        _on = _start()
        check("...and with the moment ON, the same attachment ACTS — without this pair the line "
              "above is equally satisfied by a trigger mechanism that never runs anything at all",
              os.path.exists(_mark))
        check("...and what it printed is APPENDED to the status the session was already being "
              "given, so one text both acts and reports what it did",
              "ACTED-ONCE" in _ssctx(_on) and "COST LADDER" in _ssctx(_on))
        check("...and the payload names the SOURCE, which is the whole of how an attachment tells a "
              "fresh start from a compaction: it fires on both, so 'once per project' is the "
              "attachment's own job and it needs the fact to do it",
              '"source": "startup"' in _ssctx(_on) and '"event": "session_start"' in _ssctx(_on))
        check("...and a COMPACTION says compact rather than startup — the arm that makes the field "
              "worth branching on instead of a constant dressed as a discriminator",
              '"source": "compact"' in _ssctx(_start(event="PostCompact", source="compact")))

        # THREE OUTCOMES, THREE READINGS. Failed, timed out, and ran-but-printed-nothing are three
        # different states, and an attachment whose silence cannot be told from never having run is
        # the exact defect this harness exists to prevent — one level in, at the moment that decides
        # whether any of the rest runs.
        _attach({"session_start": [{"name": "ss-quiet", "command": "true"}]})
        _q = _ssctx(_start())
        check("an attachment that RAN and printed nothing SAYS so — '(no output)' beneath a ✓, never "
              "an absence the reader is left to interpret",
              "✓ ss-quiet" in _q and "(no output)" in _q)
        _attach({"session_start": [{"name": "ss-bad", "command": "echo why-it-broke >&2; exit 3"}]})
        _b = _ssctx(_start())
        check("...one that FAILED is loud, names the error, and reads nothing like the quiet one",
              "✗ ss-bad FAILED" in _b and "why-it-broke" in _b and "(no output)" not in _b)
        _attach({"session_start": [{"name": "ss-hang", "command": "sleep 30", "timeout_sec": 1}]})
        _t0 = time.time()
        _hr = _start()
        _h = _ssctx(_hr)
        check("...and one that HANGS is BOUNDED and says TIMED OUT — a third reading, not the "
              "failure one. Unbounded, a single bad attachment would hang every session in the tree",
              "✗ ss-hang FAILED" in _h and "timed out" in _h and time.time() - _t0 < 20)
        check("...and through all three the session still gets its status and still exits 0: an "
              "attachment is an ADDITION to this moment, never a precondition for it (INV5)",
              _hr.returncode == 0
              and all("COST LADDER" in t for t in (_q, _b, _h)))

        # OBSERVABLE THAT IT FIRED, which is the same requirement the probe above carries and the
        # reason this is a trigger rather than a second hook: one registrant, one place to look.
        _attach({"session_start": [{"name": "ss-seen", "command": "echo hi"}]})
        _stn = gl(ss, "status", sid="sess-ss").stdout
        check("an attachment to this moment that has never fired is named as such in status, the "
              "same way the other three moments' are",
              "[session_start] ss-seen" in _stn and "CONFIGURED BUT NEVER FIRED" in _stn)
        _start()
        _stf = gl(ss, "status", sid="sess-ss").stdout
        check("...and once the moment has come round it reads as FIRED instead — the pair that "
              "makes the warning above mean something rather than being permanent furniture",
              "[session_start] ss-seen" in _stf and "CONFIGURED BUT NEVER FIRED" not in _stf)

        # THE FOURTH STATE, and only this moment can be in it: wired correctly, to a real moment,
        # which has been switched off. Left as "never fired" it reads as patience, and the reader
        # waits for something that cannot happen. Nobody typing session_start:false is thinking
        # about an attachment they added months earlier — remembering that is the harness's job.
        _ss_enabled(False)
        _stoff = gl(ss, "status", sid="sess-ss").stdout
        check("an attachment wired to session_start while the moment is SWITCHED OFF is told so, "
              "instead of being left reading as one that has merely not come round yet",
              "SWITCHED OFF" in _stoff and "[session_start] ss-seen" in _stoff)
        _ss_enabled(True)
        check("...and turning the moment back on stops the accusation — the pair proving that line "
              "tracks the config rather than being printed here always",
              "SWITCHED OFF" not in gl(ss, "status", sid="sess-ss").stdout)
    finally:
        shutil.rmtree(ss, ignore_errors=True)

    # Registered where it ships, not only where this repo runs it.
    with open(os.path.join(REPO, "templates", "settings.hooks.json")) as f:
        _tpl = json.load(f)["hooks"]
    check("the shipped template registers BOTH moments, so an install gets them without asking",
          all(any("sessionstart" in h.get("command", "")
                  for e in _tpl.get(ev, []) for h in e.get("hooks", []))
              for ev in ("SessionStart", "PostCompact")))

    # THE ESCAPE HATCH MUST NAME SOMETHING THAT EXISTS (#52). Every refusal told the agent to run
    # `game_loop authorize`, and game_loop is NEVER on PATH — it is a project-local binary. A
    # session that arrived through a global slash command, so it had never read CLAUDE.md or
    # llms.txt, checked PATH and the usual bin dirs, found nothing, and concluded the hatch was not
    # installed. It handed back to the human with a finished code review unposted, while the hatch
    # sat at ./.game_loop/bin/game_loop the whole time.
    #
    # Rung 2, not 6: the message already exists and is already read at exactly the right moment. It
    # only needed to name the real entry point. Documenting it harder would help only the sessions
    # that loaded CLAUDE.md — which are precisely the population that was never affected.
    # A PROMISE IN SHIPPED FILES MUST BE KEPT (#55). install.sh's closing tips and the seeded
    # templates/CLAUDE.md both told the operator to add "the shell function in the README", and the
    # README shipped no such function — two references, zero definitions, on one of the
    # highest-traffic lines in the project. Same shape as #52 one layer out: an instruction pointing
    # at something that is not there. So the promise is asserted, and the function is RUN rather
    # than eyeballed.
    # ONE SLACK APP, N REPOS (#54). notify.json was read only from the project's own .game_loop/,
    # so anyone running game_loop across several checkouts hand-copied the same bot token into every
    # one — N copies of a credential and N places to rotate it.
    # A PROJECT CAN SAY MCP WRITES ARE OFF (#53). Before this there was no way to state a policy:
    # mcp_read_only_tools resolves AMBIGUITY only by its own contract, and `authorize` is per-call.
    # So every MUTATING deny ended by telling the agent how to get the call through, and a
    # plausible-sounding workflow justification is exactly what an agent is good at producing. For a
    # project meant to touch nothing, the door has to be ABSENT rather than merely guarded.
    # THE MCP ANALOGUE OF allow_write_roots (#56). A project could gate MCP writes or remove them,
    # never declare the narrow set it trusts — so any workflow whose WORK PRODUCT lands through an
    # MCP write could not finish unattended, which is the category this tool exists to enable.
    # Observed: a completed PR review that could not be posted, where the human's authorization
    # bought a RETRY rather than a safety decision.
    # SITE CONFIG NEEDS A GITIGNORED HOME. config.json is tracked AND is the seed a fresh install
    # copies from, so a value true of one machine — a path, a tracked issue queue, a command that
    # only exists here — becomes the DEFAULT for everybody who installs from it. This project has
    # already had to remove one leak of exactly that kind from its docs. notify.json and
    # triggers.json were gitignored for this reason; config had no such home.
    print("site config has a gitignored home (config.local.json):")
    cl = make_sandbox()
    try:
        _cj = os.path.join(cl, ".game_loop", "config.json")
        _clj = os.path.join(cl, ".game_loop", "config.local.json")
        with open(_cj) as f:
            _base = json.load(f)
        _base["mcp_writes"] = "gated"        # a key status actually reports, so the override is
        with open(_cj, "w") as f:            # observable rather than merely asserted about
            json.dump(_base, f)

        # PAIRED FIRST: with no local file, the tracked value stands and nothing is announced.
        _s0 = gl(cl, "status", sid="sess-cl").stdout
        check("with no config.local.json the tracked config stands, and status says nothing about "
              "an override",
              "writes are GATED" in _s0 and "config.local.json" not in _s0)

        with open(_clj, "w") as f:
            json.dump({"mcp_writes": "disabled"}, f)
        _s1 = gl(cl, "status", sid="sess-cl").stdout
        check("config.local.json overrides the tracked file, so site wiring never has to live in "
              "the file that seeds other installs",
              "WRITES DISABLED" in _s1 and "writes are GATED" not in _s1)
        check("...and status NAMES the overridden keys — a config you cannot see is a divergence "
              "nobody can explain",
              "config.local.json" in _s1 and "mcp_writes" in _s1)
        # Malformed must not take the harness down, and must not silently win either.
        with open(_clj, "w") as f:
            f.write("{not json")
        check("...while a malformed local file is ignored rather than fatal, leaving the tracked "
              "value in force",
              "writes are GATED" in gl(cl, "status", sid="sess-cl").stdout)
        with open(os.path.join(REPO, "install.sh")) as f:
            _icl = f.read()
        check("...and install.sh ships it gitignored, like notify.json and triggers.json before it",
              "config.local.json" in _icl)

        # EVERY READER, OR IT IS A HALF-FEATURE. Shipped exactly that way once: config.local.json
        # reached `game_loop` and nothing else, so a waiting probe configured there was invisible to
        # the WATCHDOG — the one component that needed it. Structural, because the failure is a
        # NEW reader added without the merge, and no behavioural assertion catches that until
        # something silently ignores a setting somebody wrote down.
        _readers = []
        for _f in ("game_loop", "watchdog", "notify.py", "guard-writes-impl.sh",
                   "guard-mcp-impl.sh"):
            with open(os.path.join(SRC_GAME_LOOP, "bin", _f)) as fh:
                _src = fh.read()
            if "config.json" in _src and "config.local.json" in _src:
                _readers.append(_f)
        check("every component that reads config.json also reads config.local.json — a local "
              "override only SOME of them honour works where you test it and not where it matters",
              len(_readers) == 5)

        # And behaviourally, in the two that decide things: the guard and the watchdog.
        with open(_clj, "w") as f:
            json.dump({"watchdog": {"idle_sec": 99, "settle_sec": 0, "ring_cap": 7}}, f)
        _wd = subprocess.run(["python3", "-c", (
            "import importlib.util,os\n"
            "from importlib.machinery import SourceFileLoader\n"
            f"os.environ['GAME_LOOP_HOME']={os.path.join(cl, '.game_loop')!r}\n"
            f"l=SourceFileLoader('wd',{os.path.join(cl, '.game_loop', 'bin', 'watchdog')!r})\n"
            "s=importlib.util.spec_from_loader('wd',l); m=importlib.util.module_from_spec(s)\n"
            "try: l.exec_module(m)\n"
            "except SystemExit: pass\n"
            "print(m._cfg().get('ring_cap'))")], capture_output=True, text=True, env=_env(cl))
        check("...and the WATCHDOG honours it — the component whose blindness to it was the bug",
              _wd.stdout.strip() == "7")
        with open(_clj, "w") as f:
            json.dump({"mcp_writes": "disabled"}, f)
        _mg = subprocess.run([os.path.join(cl, ".game_loop", "bin", "guard-mcp.sh")],
                             input=json.dumps({"tool_name": "mcp__x__updateThing",
                                               "tool_input": {}}),
                             capture_output=True, text=True, env=_env(cl))
        check("...and the MCP guard honours it, so a policy set locally is actually enforced",
              "disabled" in _mg.stdout)
    finally:
        shutil.rmtree(cl, ignore_errors=True)

    # THE ONE QUESTION THIS INSTALLER REMEMBERS. The context cap INTERRUPTS a run somebody is
    # watching, so it is asked rather than inherited — but a prompt that fires on every install into
    # every repo is a prompt people learn to hit return through, which is indistinguishable from not
    # asking while looking exactly like consent. So the answer is remembered, and a memory is only
    # worth anything if its expiry, its precedence and its refusal to leak are all real.
    print("install.sh asks about the context cap, and remembers the answer:")
    ctxcap = tempfile.mkdtemp(prefix="ctxcap-")
    try:
        cc_home = os.path.join(ctxcap, "home")
        os.makedirs(cc_home)
        cc_answers = os.path.join(cc_home, ".game_loop", "install-answers.json")
        cc_installer = os.path.join(REPO, "install.sh")

        def cc_mk(name):
            p = os.path.join(ctxcap, name)
            os.makedirs(p)
            subprocess.run(["git", "init", "-q", "."], cwd=p, capture_output=True)
            with open(os.path.join(p, "README.md"), "w") as f:
                f.write("x\n")
            return p

        def cc_install(target, *args, home=None):
            """The REAL installer, through its real interface, with a fake HOME so the memory it
            writes lands in this test's sandbox and never in the developer's own."""
            return subprocess.run([cc_installer, *args, target], capture_output=True, text=True,
                                  input="", env=_env(HOME=home or cc_home))

        def cc_limits(p, local=True):
            f = os.path.join(p, ".game_loop",
                             "config.local.json" if local else "config.json")
            try:
                with open(f) as fh:
                    return (json.load(fh) or {}).get("limits")
            except (OSError, ValueError):
                return None

        def cc_remember(answer, tokens, days_ago):
            os.makedirs(os.path.dirname(cc_answers), exist_ok=True)
            with open(cc_answers, "w") as f:
                json.dump({"context_cap": {"answer": answer, "threshold_tokens": tokens,
                                           "asked_at": int(time.time()) - days_ago * 86400}}, f)

        # THE SEED MUST NOT CARRY A SITE'S ANSWER — and this repo shipped that leak for the length of
        # one commit. config.json is TRACKED and is the file every fresh install copies from, so
        # enabling the cap there hands the interrupting behaviour to everybody who installs from this
        # checkout, unasked, which is the precise thing the question exists to prevent. The site's
        # own answer belongs in the gitignored layer.
        with open(os.path.join(REPO, ".game_loop", "config.json")) as f:
            cc_seed = json.load(f)
        check("the SEED config ships NO context-cap answer — it is asked for, never inherited",
              "context" not in (cc_seed.get("limits") or {}))

        cc_a = cc_mk("a")
        r = cc_install(cc_a)
        check("no terminal to ask at leaves the cap off, and says so rather than going quiet",
              cc_limits(cc_a) is None and "no terminal to ask at" in r.stdout)
        check("...and remembers nothing: a silence is not an answer, and caching it as one would "
              "make the next fortnight's installs silent for a reason nobody gave",
              not os.path.exists(cc_answers))

        cc_b = cc_mk("b")
        cc_install(cc_b, "--context-cap")
        check("--context-cap enables it at the default 300K",
              cc_limits(cc_b) == {"context": {"enabled": True, "threshold_tokens": 300000}})
        check("...in the GITIGNORED layer, never in the tracked config every install copies",
              "context" not in (cc_limits(cc_b, local=False) or {}))
        cc_c = cc_mk("c")
        cc_install(cc_c, "--context-cap=200000")
        check("--context-cap=N sets the cap to N",
              (cc_limits(cc_c) or {}).get("context", {}).get("threshold_tokens") == 200000)
        check("a flag is THIS run's decision and is not remembered — otherwise one "
              "--no-context-cap would silence the question for a fortnight nobody agreed to",
              not os.path.exists(cc_answers))
        cc_d = cc_mk("d")
        cc_install(cc_d, "--no-context-cap")
        check("--no-context-cap writes nothing at all — the code default is already off, and a "
              "written `false` would read as a decision the project made",
              cc_limits(cc_d) is None)
        r = cc_install(cc_mk("bad"), "--context-cap=lots")
        check("--context-cap refuses a non-numeric cap rather than silently falling back",
              r.returncode != 0 and "token count" in (r.stdout + r.stderr))

        cc_remember("yes", 250000, 3)
        cc_e = cc_mk("e")
        r = cc_install(cc_e)
        check("a remembered YES applies with no terminal and no question, at the remembered cap",
              (cc_limits(cc_e) or {}).get("context", {}).get("threshold_tokens") == 250000)
        check("...and says WHEN it was answered and where that is kept, so it can be undone",
              "3 day(s) ago" in r.stdout and cc_answers in r.stdout)
        cc_remember("no", 300000, 2)
        cc_f = cc_mk("f")
        r = cc_install(cc_f)
        check("a remembered NO is honoured too — what is cached is the ANSWER, not a yes",
              cc_limits(cc_f) is None and "remembered" in r.stdout)
        cc_remember("yes", 250000, 20)
        cc_g = cc_mk("g")
        r = cc_install(cc_g)
        check("past the 15-day TTL the memory is not used — the question comes back",
              cc_limits(cc_g) is None and "day(s) ago" not in r.stdout
              and "no terminal to ask at" in r.stdout)
        cc_remember("yes", 250000, 1)
        cc_h = cc_mk("h")
        cc_install(cc_h, "--no-context-cap")
        check("a flag outranks a live remembered answer", cc_limits(cc_h) is None)

        # THE PROJECT'S OWN ANSWER OUTRANKS BOTH. Re-asking a tree that has already decided is how a
        # remembered yes silently turns a deliberate `false` back on at the next upgrade.
        cc_i = cc_mk("i")
        cc_install(cc_i)                                   # the remembered yes lands
        with open(os.path.join(cc_i, ".game_loop", "config.local.json")) as fh:
            _ci = json.load(fh)
        _ci["limits"]["context"] = {"enabled": False, "threshold_tokens": 111111}
        with open(os.path.join(cc_i, ".game_loop", "config.local.json"), "w") as fh:
            json.dump(_ci, fh)
        r = cc_install(cc_i)
        check("a tree that has already DECIDED is never re-asked and never overwritten, even with "
              "a live remembered yes sitting beside it",
              cc_limits(cc_i) == {"context": {"enabled": False, "threshold_tokens": 111111}}
              and "left exactly as it was" in r.stdout)
        check("...and it NAMES the file that decided, so that line is checkable rather than a claim",
              "config.local.json already decides it" in r.stdout)
        cc_j = cc_mk("j")
        cc_install(cc_j, "--no-context-cap")
        _cj = os.path.join(cc_j, ".game_loop", "config.json")
        with open(_cj) as fh:
            _c = json.load(fh)
        _c["limits"] = {"context": {"enabled": False}}
        with open(_cj, "w") as fh:
            json.dump(_c, fh)
        r = cc_install(cc_j)
        check("a decision in the TRACKED config counts as decided too — reading only the local "
              "layer would write a second, disagreeing copy of the answer",
              cc_limits(cc_j) is None and "config.json already decides it" in r.stdout)

        cc_k = cc_mk("k")
        cc_install(cc_k, "--no-context-cap")
        _ck = os.path.join(cc_k, ".game_loop", "config.local.json")
        with open(_ck, "w") as fh:
            fh.write("{ this is not json")
        r = cc_install(cc_k, "--context-cap")
        with open(_ck) as fh:
            _kept = fh.read()
        check("an unparseable local config is REFUSED, not clobbered — an unreadable config is not "
              "a config with nothing in it, and rewriting one deletes whatever it meant to say",
              "could not be read as" in r.stdout and _kept == "{ this is not json")

        # THE QUESTION ITSELF. Everything above exercises what happens when nobody is asked; this is
        # the one path where somebody is. It needs a real terminal because the installer reads from
        # /dev/tty on purpose — stdin is the SCRIPT under the documented `curl ... | bash`.
        def cc_ask(target, replies, home=None):
            import pty
            import select as _select
            env = _env(HOME=home or cc_home)
            pid, fd = pty.fork()
            if pid == 0:                                   # pragma: no cover — the child execs away
                try:
                    os.execve(cc_installer, [cc_installer, target], env)
                finally:
                    os._exit(127)
            chunks, deadline = [], time.time() + 120
            try:
                os.write(fd, replies.encode())             # canonical mode buffers until `read` asks
                while time.time() < deadline:
                    if not _select.select([fd], [], [], 1.0)[0]:
                        continue
                    try:
                        buf = os.read(fd, 65536)
                    except OSError:
                        break                              # the child closed the pty: normal exit
                    if not buf:
                        break
                    chunks.append(buf)
            finally:
                os.close(fd)
                os.waitpid(pid, 0)
            return b"".join(chunks).decode("utf-8", "replace")

        cc_home2 = os.path.join(ctxcap, "home2")            # a fresh memory, so nothing is recalled
        os.makedirs(cc_home2)
        cc_answers2 = os.path.join(cc_home2, ".game_loop", "install-answers.json")
        cc_p = cc_mk("prompt-yes")
        out = cc_ask(cc_p, "n\ny\n", home=cc_home2)         # no to skills, yes to the cap
        check("with a terminal the question is actually ASKED, and names what it costs you",
              "Turn it on?" in out and "INTERRUPTS" in out)
        check("...and a yes enables it at the default cap",
              cc_limits(cc_p) == {"context": {"enabled": True, "threshold_tokens": 300000}})
        with open(cc_answers2) as fh:
            _mem = json.load(fh)["context_cap"]
        check("...and an ANSWERED question is what gets remembered, with its TTL on the record",
              _mem["answer"] == "yes" and _mem["threshold_tokens"] == 300000
              and _mem["ttl_days"] == 15)
        os.remove(cc_answers2)
        cc_q = cc_mk("prompt-num")
        cc_ask(cc_q, "n\n120000\n", home=cc_home2)
        check("answering with a NUMBER sets that cap — one question, not two",
              (cc_limits(cc_q) or {}).get("context", {}).get("threshold_tokens") == 120000)
        os.remove(cc_answers2)
        cc_r = cc_mk("prompt-floor")
        out = cc_ask(cc_r, "n\n900\n", home=cc_home2)
        check("a cap below the floor is raised to it, out loud — a cap that low refuses every call "
              "including the ones that would raise it, which is a guard blocking its own fix",
              (cc_limits(cc_r) or {}).get("context", {}).get("threshold_tokens") == 50000
              and "below the floor" in out)
        os.remove(cc_answers2)
        cc_s = cc_mk("prompt-no")
        cc_ask(cc_s, "n\n\n", home=cc_home2)                # bare return
        check("a bare return is a no, and the no is remembered like any other answer",
              cc_limits(cc_s) is None
              and json.load(open(cc_answers2))["context_cap"]["answer"] == "no")
    finally:
        shutil.rmtree(ctxcap, ignore_errors=True)

    # A MACHINE-WIDE THIRD LAYER (~/.game_loop/config.json), on top of config.json + config.local.json.
    # Unlike config.local.json (a site override for ONE project), this is read by EVERY project's own
    # guard scripts — full install or --central — so a trust grant made once applies everywhere,
    # without hand-editing each project. The one property that makes this safe rather than a landmine:
    # trust-LIST keys UNION across all three sources instead of replacing, so a global grant can never
    # be silently erased by a project's own (possibly absent) same-key list, and vice versa.
    print("a machine-wide config layer (~/.game_loop/config.json) unions with a project's own:")
    gh = make_sandbox()          # doubles as the fake $HOME for these cases
    gw = make_sandbox()          # the project actually being guarded
    try:
        ghome_cfg = os.path.join(gh, ".game_loop", "config.json")
        gproj_cfg = os.path.join(gw, ".game_loop", "config.json")

        def _global(**kw):
            with open(ghome_cfg) as f:
                c = json.load(f)
            c.update(kw)
            with open(ghome_cfg, "w") as f:
                json.dump(c, f)

        def _proj(**kw):
            with open(gproj_cfg) as f:
                c = json.load(f)
            c.update(kw)
            with open(gproj_cfg, "w") as f:
                json.dump(c, f)

        def _mcp_call(tool, home=None):
            r = subprocess.run([os.path.join(gw, ".game_loop", "bin", "guard-mcp.sh")],
                               input=json.dumps({"tool_name": tool, "session_id": "sess-gh",
                                                 "tool_input": {}}),
                               capture_output=True, text=True,
                               env=_env(gw, sid="sess-gh", HOME=home or gh))
            try:
                d = json.loads(r.stdout)["hookSpecificOutput"]
                return d["permissionDecision"], d.get("permissionDecisionReason", "")
            except Exception:  # noqa: BLE001 — no JSON means it allowed
                return "allow", ""

        _global(mcp_trusted_servers=["mcp__examplesrv__"], mcp_writes="gated")
        _proj(mcp_writes="gated")
        check("a server trusted only in the GLOBAL config is trusted for a project with no opinion "
              "on it at all",
              _mcp_call("mcp__examplesrv__say")[0] == "allow")

        # mcp_standing_writes only ever applies to a verb ALREADY classified MUTATING (checked as an
        # `elif first in MUTATE_VERBS` — see guard-mcp-impl.sh's verdict section) — unlike
        # mcp_trusted_servers just above, which is unconditional. "update" is a real MUTATE_VERBS
        # entry; a made-up verb here would silently test the unclassifiable path instead.
        _global(mcp_standing_writes=["mcp__global_own__update"])
        _proj(mcp_standing_writes=["mcp__project_own__update"])
        check("global mcp_standing_writes and a project's own DIFFERENT list both take effect — "
              "union, not replace, in the direction that would otherwise erase the global grant",
              _mcp_call("mcp__global_own__update")[0] == "allow")
        check("...and the same call proves it in the OTHER direction — the project's own entry "
              "survives a global file that never mentioned it",
              _mcp_call("mcp__project_own__update")[0] == "allow")
        check("...while an undeclared tool with the same mutating verb is still refused — union "
              "widens trust, it does not turn off classification",
              _mcp_call("mcp__nobody_trusts__update")[0] == "deny")

        _global(mcp_writes="disabled")
        _proj(mcp_writes="gated", mcp_standing_writes=[])
        _d1, _r1 = _mcp_call("mcp__unclassifiable__thing")
        check("a SCALAR key still uses normal override, not union: the project's own mcp_writes "
              "wins over the global default",
              _d1 == "deny" and "disabled" not in _r1)

        _global(mcp_writes="gated")
        _proj(mcp_writes="disabled")
        _d2, _r2 = _mcp_call("mcp__unclassifiable__thing")
        check("...checked in both directions — the project's stricter setting also wins when it is "
              "the one that differs",
              _d2 == "deny" and 'mcp_writes: "disabled"' in _r2)

        os.remove(ghome_cfg)
        _proj(mcp_writes="gated", mcp_standing_writes=["mcp__project_own__update"])
        check("with no ~/.game_loop/config.json at all, behavior is unchanged from a plain "
              "config.json + config.local.json merge",
              _mcp_call("mcp__project_own__update")[0] == "allow"
              and _mcp_call("mcp__nobody_trusts__update")[0] == "deny")

        with open(ghome_cfg, "w") as f:
            f.write("{not valid json")
        check("...and a MALFORMED global config degrades to 'as if absent' rather than taking the "
              "guard down",
              _mcp_call("mcp__project_own__update")[0] == "allow")

        # The write guard's OWN CONFIG_MERGED reader is a separate, identical block in a different
        # file (guard-writes-impl.sh) — prove it independently rather than assuming the guard-mcp
        # result generalizes.
        with open(ghome_cfg, "w") as f:
            json.dump({"allow_write_roots": [os.path.join(gh, "global-scratch")]}, f)
        with open(gproj_cfg) as f:
            _pc = json.load(f)
        _pc["allow_write_roots"] = [os.path.join(gw, "project-scratch")]
        with open(gproj_cfg, "w") as f:
            json.dump(_pc, f)
        os.makedirs(os.path.join(gh, "global-scratch"), exist_ok=True)
        os.makedirs(os.path.join(gw, "project-scratch"), exist_ok=True)

        def _write_call(path):
            return subprocess.run([os.path.join(gw, ".game_loop", "bin", "guard-writes.sh")],
                                  input=json.dumps({"tool_name": "Write",
                                                    "tool_input": {"file_path": path}}),
                                  capture_output=True, text=True,
                                  env=_env(gw, sid="sess-gh", HOME=gh))

        check("the write guard's own config merge unions allow_write_roots the same way — a "
              "globally-allowed root ...",
              not denied(_write_call(os.path.join(gh, "global-scratch", "f.txt"))))
        check("...and the project's OWN allowed root, together, neither shadowing the other",
              not denied(_write_call(os.path.join(gw, "project-scratch", "f.txt"))))
    finally:
        shutil.rmtree(gh, ignore_errors=True)
        shutil.rmtree(gw, ignore_errors=True)

    print("a project can declare the narrow set of MCP writes it trusts (#56):")
    sw = make_sandbox()
    try:
        _scfg = os.path.join(sw, ".game_loop", "config.json")

        def _set(**kw):
            with open(_scfg) as f:
                c = json.load(f)
            c.update(kw)
            with open(_scfg, "w") as f:
                json.dump(c, f)

        def _call(tool, ti=None):
            r = subprocess.run([os.path.join(sw, ".game_loop", "bin", "guard-mcp.sh")],
                               input=json.dumps({"tool_name": tool, "session_id": "sess-sw",
                                                 "tool_input": ti or {"id": 1}}),
                               capture_output=True, text=True, env=_env(sw, sid="sess-sw"))
            try:
                d = json.loads(r.stdout)["hookSpecificOutput"]
                return d["permissionDecision"], d["permissionDecisionReason"]
            except Exception:  # noqa: BLE001 — no JSON means it allowed
                return "allow", ""

        _DECL = "mcp__github__createPullRequestReview"
        # PAIRED FIRST: undeclared, it is refused — so "allowed" below is the policy, not the guard
        # having stopped classifying.
        _set(mcp_writes="gated", mcp_standing_writes=[])
        check("an undeclared mutating call is still refused, so the allowance below means something",
              _call(_DECL)[0] == "deny")

        _set(mcp_standing_writes=[_DECL])
        check("a DECLARED tool runs with no human — the standing, reviewed policy allow_write_roots "
              "already gives the filesystem plane",
              _call(_DECL)[0] == "allow")
        with open(os.path.join(sw, ".game_loop", "log.jsonl")) as f:
            check("...and the consumption is LOGGED, so 'allowed by policy' and 'never ran' stay "
                  "distinguishable afterwards",
                  any('"standing_mcp_write"' in ln and _DECL in ln for ln in f))
        check("...and status lists the standing doors, so a human sees what is open without "
              "reading config",
              _DECL in gl(sw, "status", sid="sess-sw").stdout)

        # THE FOUR REFUSALS THAT KEEP IT A POLICY. Each is a way it could have become an off switch.
        check("an ARGUMENT-level mutation is refused even for a declared tool — that finding is "
              "per-call and cannot be pre-approved",
              _call(_DECL, {"sql": "DELETE FROM t"})[0] == "deny"
              and _call(_DECL, {"force": True})[0] == "deny")
        _set(mcp_writes="disabled")
        check("...and the whole list is inert under mcp_writes disabled, so #53 stays absolute",
              _call(_DECL)[0] == "deny")
        _set(mcp_writes="gated", mcp_standing_writes=["mcp__github__deletePullRequestComment"])
        _hd, _hr = _call("mcp__github__deletePullRequestComment")
        check("...and an entry naming an IRREVERSIBLE verb is refused AT CONFIG-READ time, loudly, "
              "rather than silently dropped",
              _hd == "deny" and "irreversible verb" in _hr)
        _set(mcp_standing_writes=["mcp__github__bogus", "mcp__github__*"])
        _pd, _pr = _call("mcp__github__getThing")
        check("...and an invalid policy stops classification entirely rather than honouring the "
              "valid half, because nobody could tell which half was live",
              _pd == "deny" and "not a valid policy" in _pr)

        # ── mcp_trusted_servers: THIS SERVER IS OURS ──────────────────────────────────────────────
        #
        # Reported by a user whose team WROTE the MCP server their agents call: every fresh
        # PR-review agent stopped to ask for the same approve/comment authorizations, on a server
        # the project owns. mcp_standing_writes covers that and deliberately stops short of the
        # irreversible and landing tiers (#57) — right for somebody else's server, wrong for one you
        # wrote, where the team already owns the blast radius.
        _set(mcp_writes="gated", mcp_standing_writes=[], mcp_trusted_servers=["mcp__github__"])
        for _t in ("mcp__github__approvePullRequest", "mcp__github__mergePullRequest",
                   "mcp__github__deleteIssueComment", "mcp__github__forcePushBranch"):
            check(f"a wholly trusted server allows {_t.split('__')[-1]} — including the tiers a "
                  "standing prefix withholds, which is the whole request",
                  _call(_t)[0] == "allow")
        check("...including a mutating ARGUMENT, since 'everything' stated plainly beats a "
              "half-grant that refuses what you most likely built the server to do",
              _call("mcp__github__addIssueComment", {"sql": "DELETE FROM t"})[0] == "allow")
        with open(os.path.join(sw, ".game_loop", "log.jsonl")) as f:
            check("...and every consumption is logged as trusted_mcp_write naming the server, so "
                  "'allowed because ours' and 'never ran' stay distinguishable",
                  '"trusted_mcp_write"' in f.read())
        # THE CONTROLS. Each is a way this could have become an off switch rather than a policy.
        check("...while a server NOBODY declared is refused exactly as before — the grant is one "
              "server, not a mood",
              _call("mcp__other__deleteThing")[0] == "deny")
        _set(mcp_writes="disabled")
        check("...and mcp_writes disabled still wins absolutely, so #53 outranks even this",
              _call("mcp__github__mergePullRequest")[0] == "deny")
        _set(mcp_writes="gated", mcp_trusted_servers=[])
        check("...and with the key absent the floors are untouched, so the allowances above are "
              "the declaration talking and not a guard that quietly stopped classifying",
              _call("mcp__github__deleteIssueComment")[0] == "deny")
        # SERVER GRAIN ONLY: "trust everything" is a statement about a server, never about a tool.
        for _bad, _why in ((["mcp__github__mergeIt"], "must name a WHOLE SERVER"),
                           (["mcp__*__"], "globs are never accepted"),
                           (["mcp____"], "must name a WHOLE SERVER")):
            _set(mcp_trusted_servers=_bad)
            _d, _r = _call("mcp__github__getThing")
            check(f"...and {_bad[0]!r} is refused at CONFIG-READ, not silently dropped — the widest "
                  "door half-honoured is worse than one rejected",
                  _d == "deny" and "not a valid policy" in _r)
        _set(mcp_trusted_servers=["mcp__github__"])
        check("...and status reports it in capitals, because a door this wide that nobody can see "
              "is the failure this tool argues against",
              "WHOLLY TRUSTED SERVER" in gl(sw, "status", sid="sess-sw").stdout)
        _set(mcp_trusted_servers=[])

        # ── #57: THE SERVER GRAIN ─────────────────────────────────────────────────────────────────
        #
        # #56 shipped exact names only and this suite asserted a prefix was refused. That assertion
        # is REVERSED here deliberately: when a project's MCP servers are its own first-party code,
        # an enumeration goes stale every time the server grows a tool, and it goes stale toward a
        # DEAD-ENDED agent — the failure #56 was filed to remove, on a slower clock. The reversal is
        # safe for a reason this file can check rather than assert: every floor below runs on the
        # LIVE call and returns before standing is consulted, so a prefix widens which SERVERS are
        # trusted and cannot widen WHAT may be done through them. The arms below are that claim.
        _set(mcp_standing_writes=["mcp__github__"])
        check("a whole-server PREFIX grants a mutating tool on that server — the unit of trust for "
              "first-party code is the server, and an enumeration of its tools rots (#57)",
              _call("mcp__github__addIssueComment")[0] == "allow")
        with open(os.path.join(sw, ".game_loop", "log.jsonl")) as f:
            _lg = f.read()
        check("...and the log records WHICH grain allowed it, so an audit can review the rule and "
              "not merely observe that something permitted the call",
              '"grain": "prefix"' in _lg and '"via": "mcp__github__"' in _lg)
        check("...while a tool on a server nobody granted is still refused — the prefix is a grant "
              "over one server, not a mood",
              _call("mcp__other__addThing")[0] == "deny")

        # THE TIER A PREFIX DOES NOT INHERIT. merge/publish/deploy/release/push sit in MUTATE_VERBS,
        # so under exact names granting one was an act of typing it out. Under a prefix it would be
        # inherited from which set a verb happens to occupy — and that is the difference between an
        # agent posting its review unattended and an agent LANDING CODE unattended.
        _md, _mr = _call("mcp__github__mergePullRequest")
        check("...and a LANDING verb is refused through a prefix — a rule written once, against "
              "tools that did not exist yet, does not get to move work into the world",
              _md == "deny" and "LANDS work" in _mr)
        check("...and the refusal names the exact grant that would allow it, so the human is left "
              "with a next step rather than a wall",
              "Name this tool EXACTLY" in _mr)
        _set(mcp_standing_writes=["mcp__github__mergePullRequest"])
        check("...while the SAME tool named EXACTLY is allowed — typing it out is the deliberate "
              "act #56 exists to respect, and the asymmetry is the whole point",
              _call("mcp__github__mergePullRequest")[0] == "allow")

        # THE FLOORS STILL RUN UNDER A PREFIX, which is what makes the grain safe at all.
        _set(mcp_standing_writes=["mcp__github__"])
        check("an IRREVERSIBLE verb is refused through a prefix, though no config-read check could "
              "have seen it — the surviving layer is the one that runs on every call",
              _call("mcp__github__deleteIssueComment")[0] == "deny")
        check("...and an ARGUMENT-level mutation is refused through a prefix too, since that "
              "finding is per-call and was never pre-approvable",
              _call("mcp__github__addIssueComment", {"sql": "DELETE FROM t"})[0] == "deny")

        # A STATE-CHANGING VERB NOBODY ENUMERATED used to fail closed, which stranded the agent
        # rather than protecting anything: a review could post its comments (`add`, `create`,
        # `approve`) but not resolve the threads it had just verified. These verbs are ordinary
        # members of MUTATE_VERBS, so they reach the policy instead of dying before it.
        for _t in ("resolveComment", "minimizePullRequestComment", "unminimizePullRequestComment",
                   "convertToDraft"):
            check(f"a state-changing verb reaches the standing policy rather than failing closed: "
                  f"{_t}",
                  _call("mcp__github__" + _t)[0] == "allow")
        # PAIRED: being classified is not being allowed. The same verbs are refused the moment the
        # policy stops covering them — otherwise the checks above would pass for the wrong reason.
        _set(mcp_standing_writes=[])
        check("...and with the prefix withdrawn every one of them is refused again, so the allow "
              "above is the POLICY talking and not a verb that quietly became free",
              all(_call("mcp__github__" + _t)[0] == "deny"
                  for _t in ("resolveComment", "minimizePullRequestComment", "convertToDraft")))
        _set(mcp_standing_writes=["mcp__github__"])

        # A verb slot too generic to whitelist globally STAYS ambiguous: `user` and the leading
        # token of `systemPrompt` are real words in read-only names, so they keep failing closed
        # and a project names the exact tool in mcp_read_only_tools if it wants them.
        _ud, _ur = _call("mcp__github__userInfo")
        check("a verb slot too generic to classify still fails closed even under a prefix — the "
              "grant widens which servers are trusted, never what counts as a known verb",
              _ud == "deny" and "could not be classified" in _ur)

        # The same gap, one layer worse, on a server whose tools repeat its name
        # (`mcp__device__device_build`): the repeat is stripped first, so the REAL verb slot is what
        # gets classified — and on a first-party device server that slot is a physical effector.
        _set(mcp_standing_writes=["mcp__device__"])
        for _t in ("device_build", "device_background", "device_foreground", "device_screenshot",
                   "device_key", "device_pointer", "device_setup"):
            check(f"a device effector past the server-name repeat reaches the policy: {_t}",
                  _call("mcp__device__" + _t)[0] == "allow")
        check("...while a LANDING verb on that same server is still refused through the prefix, so "
              "widening the effectors did not widen what may be shipped to a device",
              _call("mcp__device__device_deploy")[0] == "deny")
        _set(mcp_standing_writes=["mcp__github__"])

        _set(mcp_writes="disabled")
        check("...and a prefix is inert under mcp_writes disabled, so #53 stays absolute across "
              "both grains rather than just the one it was written against",
              _call("mcp__github__addIssueComment")[0] == "deny")

        # NO THIRD GRAIN. Server or exact tool; anything between is a shape nobody can audit fast.
        # Note `mcp__github__get` is NOT that shape -- it is a valid exact tool name and nothing in
        # the string distinguishes it from one. Only a trailing `__` declares prefix intent, so the
        # sub-server grain is `mcp__github__get__`, and that is what must be refused.
        _set(mcp_writes="gated", mcp_standing_writes=["mcp__github__get__"])
        _gd, _gr = _call("mcp__github__getThing")
        check("a prefix BELOW the server level is refused at config-read — two grains are "
              "reviewable at a glance and three are not",
              _gd == "deny" and "neither grain" in _gr)
        _set(mcp_standing_writes=["mcp____"])
        _ed, _er = _call("mcp__github__getThing")
        check("...and a prefix naming NO server is refused rather than read as every server",
              _ed == "deny" and "must NAME a server" in _er)
    finally:
        shutil.rmtree(sw, ignore_errors=True)

    print("a project can turn MCP writes off (#53):")
    mp = make_sandbox()
    try:
        _mcfg = os.path.join(mp, ".game_loop", "config.json")

        def _policy(v):
            with open(_mcfg) as f:
                c = json.load(f)
            if v is None:
                c.pop("mcp_writes", None)
            else:
                c["mcp_writes"] = v
            with open(_mcfg, "w") as f:
                json.dump(c, f)

        def _mcp(tool, ti=None):
            r = subprocess.run([os.path.join(mp, ".game_loop", "bin", "guard-mcp.sh")],
                               input=json.dumps({"tool_name": tool, "session_id": "sess-mcp",
                                                 "tool_input": ti or {"id": 1}}),
                               capture_output=True, text=True, env=_env(mp, sid="sess-mcp"))
            try:
                d = json.loads(r.stdout)["hookSpecificOutput"]
                return d["permissionDecision"], d["permissionDecisionReason"]
            except Exception:  # noqa: BLE001 — no JSON at all means it allowed
                return "allow", ""

        # DEFAULT IS UNCHANGED, asserted first so "disabled works" means something.
        _policy(None)
        _d, _r = _mcp("mcp__x__deleteThing")
        check("with no policy set, a mutating call is refused AND offered the hatch — today's "
              "behaviour, unchanged on upgrade",
              _d == "deny" and "authorize" in _r)
        _policy("gated")
        _d2, _r2 = _mcp("mcp__x__deleteThing")
        check("...and an explicit 'gated' is the same thing said out loud",
              _d2 == "deny" and "authorize" in _r2)

        _policy("disabled")
        _dd, _rr = _mcp("mcp__x__deleteThing")
        check("with mcp_writes disabled a mutating call is refused with NO remedy offered — the "
              "door is absent rather than guarded",
              _dd == "deny" and "authorize" not in _rr and "disabled" in _rr)
        check("...and it says asking is not the next step, since the human turned it off on purpose",
              "not the next step" in _rr)
        # READ-ONLY IS UNTOUCHED. "disabled" is about writes; blocking reads would make it a
        # different feature that nobody asked for.
        check("...while a read-only call still passes, because this is a policy about WRITES",
              _mcp("mcp__x__getThing")[0] == "allow")

        # An unrecognised value must not silently do something else. Checked HERE, before any
        # authorization exists — later it would be allowed by the live grant and the assertion would
        # fail for a reason that is not the policy.
        _policy("disabledd")
        _td, _tr = _mcp("mcp__x__deleteThing")
        check("a typo'd policy reads as GATED — the error runs toward today's behaviour, and status "
              "names the value so the typo is visible rather than silent",
              _td == "deny" and "authorize" in _tr
              and "disabledd" in gl(mp, "status", sid="sess-mcp").stdout)
        _policy("disabled")

        # THE ONE THAT MAKES IT REAL: an existing authorization must not open a disabled project,
        # or the config declared a policy the guard quietly did not hold.
        # Granted through the REAL verb rather than by writing state by hand: a fixture that
        # invents the record's shape tests the fixture. (It did — my first attempt used a key the
        # implementation does not have, and the paired arm failed for that reason rather than the
        # interesting one.)
        gl(mp, "authorize", "--path", "mcp__x__deleteThing", "--reason", "the human said so",
           "--uses", "3", sid="sess-mcp")
        _ad, _ar = _mcp("mcp__x__deleteThing")
        check("...and a LIVE authorization cannot open it — disabled means disabled, or the setting "
              "is worse than not having it",
              _ad == "deny" and "authorize" not in _ar)
        # PAIRED: that same authorization DOES open a gated project, so the arm above is not passing
        # because authorizations are broken.
        _policy("gated")
        check("...while the same authorization opens a GATED project, so the refusal above is the "
              "policy and not a broken hatch",
              _mcp("mcp__x__deleteThing")[0] == "allow")
        _policy("disabled")

        check("...and status says plainly when the door has been removed",
              "WRITES DISABLED" in gl(mp, "status", sid="sess-mcp").stdout)
    finally:
        shutil.rmtree(mp, ignore_errors=True)

    print("notify config falls back to a user-level file (#54):")
    nf = make_sandbox()
    _xdg = tempfile.mkdtemp(prefix="glxdg-")
    try:
        _uf = os.path.join(_xdg, "game_loop", "notify.json")
        os.makedirs(os.path.dirname(_uf))
        with open(_uf, "w") as f:
            json.dump({"slack": {"webhook_url": "https://example.invalid/user"}}, f)

        def _nstatus():
            return subprocess.run([os.path.join(nf, ".game_loop", "bin", "game_loop"), "status"],
                                  capture_output=True, text=True,
                                  env=_env(nf, sid="sess-nf", XDG_CONFIG_HOME=_xdg)).stdout

        _u = _nstatus()
        check("with no project notify.json, the USER-level file is used — one app, N repos",
              "Slack configured" in _u)
        check("...and status names WHICH file, since a user-level config pages for every project on "
              "the machine and 'where is this going' must be answerable",
              "user-level, shared by every project here" in _u)

        # THE PROJECT STILL WINS, WHOLE. Not merged key-by-key: a half-inherited credential is the
        # worst outcome here — a project pointing at its own channel would silently borrow the
        # user's token, and a paging path that works for the wrong reason is the hardest to notice.
        with open(os.path.join(nf, ".game_loop", "notify.json"), "w") as f:
            json.dump({"slack": {"webhook_url": "https://example.invalid/project"}}, f)
        _p = _nstatus()
        check("...while a project notify.json overrides it entirely, and status stops citing the "
              "user file",
              "Slack configured" in _p and "user-level" not in _p)

        # PAIRED: neither file, and it must still say so rather than looking configured.
        os.remove(os.path.join(nf, ".game_loop", "notify.json"))
        os.remove(_uf)
        _n = _nstatus()
        check("...and with neither file it reports NOT configured, naming both places to put one",
              "not configured" in _n and ".config/game_loop/notify.json" in _n)
    finally:
        shutil.rmtree(nf, ignore_errors=True)
        shutil.rmtree(_xdg, ignore_errors=True)

    print("the shell function two shipped files promise actually exists (#55):")
    with open(os.path.join(REPO, "README.md")) as f:
        _rm = f.read()
    _fn = re.search(r"```bash\n(game_loop\(\) \{.*?\n\})\n```", _rm, re.S)
    check("the README defines the function install.sh and templates/CLAUDE.md send people to",
          _fn is not None)
    with open(os.path.join(REPO, "install.sh")) as f:
        _ish55 = f.read()
    with open(os.path.join(REPO, "templates", "CLAUDE.md")) as f:
        _tcm = f.read()
    check("...and both files still point at it, so this stays a checked promise rather than a "
          "coincidence",
          "shell function" in _ish55 and "shell function" in _tcm)
    if _fn:
        _fd = tempfile.mkdtemp(prefix="glfn-")
        try:
            _fs = os.path.join(_fd, "fn.sh")
            with open(_fs, "w") as f:
                f.write(_fn.group(1) + "\n")
            _deep = os.path.join(_fd, "a", "b")
            os.makedirs(_deep)
            _miss = subprocess.run(["bash", "-c", f". {_fs}; game_loop status"], cwd=_deep,
                                   capture_output=True, text=True)
            check("...and with no harness above it, it FAILS with a named reason rather than "
                  "silently doing nothing",
                  _miss.returncode == 127 and "not a global command" in _miss.stderr)
            _hit = subprocess.run(["bash", "-c", f". {_fs}; game_loop confidence"],
                                  cwd=os.path.join(REPO, "docs"), capture_output=True, text=True)
            check("...and from a SUBDIRECTORY of a guarded repo it resolves that repo's binary — "
                  "walking up is what keeps it correct in a subdir and in a linked worktree",
                  _hit.returncode == 0 and "CONFIDENCE:" in _hit.stdout)
        finally:
            shutil.rmtree(_fd, ignore_errors=True)

    print("a superseded watchdog stands down (the sweep's own KNOWN GAP, measured at 0 kills):")
    # THE SWEEP NAMED THIS ONE AND SAID SO: `watchdog::superseded` neutered to `return False` killed
    # NOTHING out of a 534-assertion baseline. Every ring decision that branches on "has a newer
    # watchdog taken over?" was unasserted, so a broken one would leave TWO watchdogs ringing the
    # same session and no test anywhere would notice. It sat in NOT_SWEPT as declared debt with the
    # remedy written down — "an assertion nobody has written yet" — which is what this is.
    sp = make_sandbox()
    try:
        _ssd = os.path.join(sp, ".game_loop", "sessions", "sess-sup")
        os.makedirs(_ssd, exist_ok=True)
        _spid = os.path.join(_ssd, ".watchdog.pid")
        _slog = os.path.join(sp, ".game_loop", "log.jsonl")
        _stp = os.path.join(sp, "transcript.jsonl")
        with open(_stp, "w") as f:
            f.write("{}\n")
        with open(os.path.join(_ssd, "state.json"), "w") as f:
            json.dump({"mandate": {"active": True, "text": "keep going", "at": "now"},
                       "watchdog_rings": 0, "watchdog_last_ring_size": 0}, f)

        # A SACRIFICIAL PID, never this process's own. claim_pidfile() SIGTERMs whatever pid it
        # finds — that is its job — so writing os.getpid() here makes the next watchdog invocation
        # kill the test runner. It did: the suite died at exit 143 with no output, which reads
        # exactly like a hang. Hand the killer something it is allowed to kill.
        _victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])

        def _steal():
            """Claim the pidfile out from under the running watchdog, as a newer one would.

            The REAL path is claim_pidfile() killing the old process, so superseded() is the
            backstop for when that kill does not land — a race, or a pid that has already gone. It
            is exactly the branch a signal-based test would never reach, which is a fair guess at
            why it went unasserted for so long.
            """
            for _ in range(200):
                try:
                    with open(_spid) as f:
                        if f.read().strip().isdigit():
                            break
                except OSError:
                    pass
                time.sleep(0.05)
            with open(_spid, "w") as f:
                f.write(str(_victim.pid))          # alive, certainly not the watchdog, expendable

        _t = threading.Thread(target=_steal, daemon=True)
        _t.start()
        _sup = run_watchdog(os.path.join(sp, ".game_loop", "bin", "watchdog"),
                            {"session_id": "sess-sup", "transcript_path": _stp},
                            WATCHDOG_IDLE_SEC="6", WATCHDOG_SETTLE_SEC="0")
        _t.join(timeout=5)
        with open(_slog) as f:
            _sl = f.read()
        check("a watchdog whose pidfile has been claimed by another process does NOT ring — two "
              "watchdogs waking one session is the failure the pidfile exists to prevent",
              _sup.returncode == 0)
        check("...and it SAYS it was superseded, so standing down is distinguishable from a "
              "watchdog that simply broke",
              '"watchdog_quiet"' in _sl and "superseded" in _sl)

        # PAIRED, or the quiet above proves only that the watchdog stopped working: the identical
        # idle session, pidfile left alone, must still ring.
        open(_slog, "w").close()
        with open(os.path.join(_ssd, "state.json"), "w") as f:
            json.dump({"mandate": {"active": True, "text": "keep going", "at": "now"},
                       "watchdog_rings": 0, "watchdog_last_ring_size": 0}, f)
        _ring = run_watchdog(os.path.join(sp, ".game_loop", "bin", "watchdog"),
                             {"session_id": "sess-sup", "transcript_path": _stp},
                             WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0")
        check("...while the SAME idle session with its pidfile untouched rings — the quiet is "
              "supersession doing its job, not the watchdog having stopped",
              _ring.returncode == 2)
    finally:
        try:
            _victim.kill()
        except (NameError, OSError):
            pass
        shutil.rmtree(sp, ignore_errors=True)

    print("prose arrives unmangled, or it does not arrive (#58):")
    pw = make_sandbox()
    try:
        _long = "x" * 450
        _f = os.path.join(pw, "prose.txt")
        with open(_f, "w") as f:
            f.write(_long)

        _over = gl(pw, "note", "--text", _long, sid="sess-prose")
        check("an inline prose value past the measured bound is REFUSED — a quoted shell argument "
              "is code to the shell first, and a sentence with words eaten out of it is just a "
              "shorter sentence by the time this tool sees it",
              _over.returncode != 0 and "must come from a file" in _over.stderr)
        check("...and the refusal explains the MECHANISM rather than reading as a style rule, since "
              "a limit that looks arbitrary gets met with a shorter sentence instead of a file",
              "backticks" in _over.stderr and "$NAME" in _over.stderr)
        check("...and it states what it does NOT fix: a short value with a backtick is still "
              "mangled and still accepted",
              "does not remove it" in _over.stderr)

        # PAIRED: the same text, same length, through the file — or the bound is just a wall.
        _via = gl(pw, "note", "--text-file", _f, sid="sess-prose")
        check("...while the SAME text through --text-file is accepted, so the bound is a redirect "
              "and not a cap on how much you may say",
              _via.returncode == 0)
        with open(os.path.join(pw, ".game_loop", "log.jsonl")) as f:
            check("...and the file's content is what got recorded, not its path",
                  any(_long in ln for ln in f))

        _both = gl(pw, "note", "--text", "short", "--text-file", _f, sid="sess-prose")
        check("...and setting both is refused — two sources for one value is the shape where "
              "nobody can say afterwards which one was recorded",
              _both.returncode != 0 and "are both set" in _both.stderr)
        _miss = gl(pw, "note", "--text-file", os.path.join(pw, "nope.txt"), sid="sess-prose")
        check("...and an unreadable file is refused rather than recorded as an empty field, which "
              "would read forever after as 'they left it blank'",
              _miss.returncode != 0 and "cannot read" in _miss.stderr)

        # THE FILE OPTION IS DERIVED, NOT LISTED. The defect being fixed is a rule that depended on
        # somebody remembering, so the safe path must not depend on somebody remembering either.
        _src = open(os.path.join(SRC_GAME_LOOP, "bin", "game_loop")).read()
        _pairs = re.search(r"^PROSE_OPTS = \((.*?)\)$", _src, re.S | re.M)
        _opts = re.findall(r'"(--[a-z-]+)"', _pairs.group(1)) if _pairs else []
        check("every prose option is declared in one place, and there are several — a list of one "
              "would make the sweep below prove nothing",
              len(_opts) >= 8)
        _helps = gl(pw, "harden", "--help", sid="sess-prose").stdout
        check("...and each prose option a verb takes gets its file sibling by WALKING the parsers, "
              "so a verb that later reuses --notes is covered without anyone adding a pair",
              all(o + "-file" in _helps for o in ("--learning", "--mechanism", "--general")))

        # THE LIST ITSELF WENT STALE, which is what an enumeration does. The first version missed
        # `mandate --set` — the sentence the whole autonomy loop is driven by, and the most
        # expensive one in the tool to have three words eaten out of — plus the arm's question and
        # prediction and every descriptive field of the evidence family. Found by trying to rewrite
        # this repo's own mandate through a file and being told the option did not exist. Naming the
        # load-bearing ones here is not a substitute for the walk above; it is a floor under the one
        # thing the walk cannot notice, which is a field nobody put on the list.
        for _verb, _opt in (("mandate", "--set"), ("arm", "--question"), ("arm", "--predict"),
                            ("effector", "--known-state"), ("instrument", "--measures"),
                            ("pin", "--restore")):
            _h = gl(pw, _verb, "--help", sid="sess-prose").stdout
            check(f"...and `{_verb} {_opt}` — a sentence somebody composed — can come from a file",
                  _opt + "-file" in _h)
        # PAIRED: a field that is a PATH, a NAME or an ENUM must NOT be bounded, or the rule starts
        # refusing legitimate values to no purpose. None of these can carry a backtick that means
        # anything, so none of them belongs in PROSE_OPTS.
        _h = gl(pw, "claim", "--help", sid="sess-prose").stdout
        check("...while --read (a path) and --outcome (an enum) get no file twin — the entry test "
              "is whether the field holds a composed sentence, not whether it holds a string",
              "--read-file" not in _h and "--outcome-file" not in _h)

        # AND THE MANDATE ITSELF ROUND-TRIPS, since that is the field the miss was found on.
        _mf = os.path.join(pw, "mandate.txt")
        with open(_mf, "w") as f:
            f.write("keep going until the queue is empty")
        gl(pw, "mandate", "--set-file", _mf, sid="sess-prose")
        check("...and a mandate SET from a file is the mandate that comes back out — the loop's own "
              "prose no longer has to survive a shell to be recorded",
              "keep going until the queue is empty" in gl(pw, "status", sid="sess-prose").stdout)
        check("...and the bound in the refusal is the constant, not a number retyped into prose",
              "PROSE_MAX = 400" in _src and "400 chars" in _helps)
    finally:
        shutil.rmtree(pw, ignore_errors=True)

    print("a session that arrived blind is oriented ONCE (#60):")
    ow = make_sandbox()
    try:
        def _refuse(sid):
            r = subprocess.run([os.path.join(ow, ".game_loop", "bin", "guard-writes.sh")],
                               input=json.dumps({"tool_name": "Write", "session_id": sid,
                                                 "tool_input": {"file_path": "/etc/nope"}}),
                               capture_output=True, text=True, env=_env(ow, sid=sid))
            d = json.loads(r.stdout)["hookSpecificOutput"]
            return d["permissionDecisionReason"]

        # THE REFUSAL IS THE ONBOARDING SURFACE, because work here starts as user-level slash
        # commands and a session that never loaded the project's CLAUDE.md is the NORMAL case.
        # Reported: the agent hit a refusal, probed five global bin dirs for a `game_loop` on PATH,
        # found none because nothing looks UP-TREE for a project-local binary, concluded "not
        # installed" and escalated. #52's absolute path removes the dead end; it does not tell a
        # blind agent that there is anything to read.
        _first = _refuse("sess-blind")
        check("a blind session's FIRST refusal carries a pointer — one line, naming the harness and "
              "where to read about it",
              "first refusal in this session" in _first)
        _brief = os.path.join(ow, ".game_loop", "llms.txt")
        check("...and every path that pointer names EXISTS — prose cannot satisfy this repo's own "
              "keystone, and the first version of this line named a brief no consumer had",
              _brief in _first and os.path.isfile(_brief)
              and os.path.isfile(os.path.join(ow, ".game_loop", "bin", "game_loop")))
        check("...so the installer ships the agent brief; an installed project used to contain "
              "NOTHING written for the agent to read",
              os.path.isfile(_brief) and "game_loop" in open(_brief).read())

        # BOUNDED BY SESSION, NOT BY REFUSAL. Each session hits its first refusal exactly once, so
        # the ceiling is one line per session lifetime rather than one per event — which is what
        # keeps this from being N copies of noise in a checkout running many sessions.
        check("...and the SECOND refusal in that session is silent — the bound is per session, not "
              "per refusal, so a busy run is not narrated at",
              "first refusal in this session" not in _refuse("sess-blind"))

        # RUNNING status IS ORIENTATION. A session that arrived the documented way has already read
        # everything the pointer would point at, so it never sees it.
        gl(ow, "status", sid="sess-oriented")
        check("...while a session that ran `status` first is never oriented at all — the pointer is "
              "aimed at sessions that arrived blind, which is the whole population it is for",
              "first refusal in this session" not in _refuse("sess-oriented"))
        # PAIRED, so the silence above is `status` doing it rather than the line having stopped
        # working: a third, untouched session in the SAME install still gets it.
        check("...and a third session in the same install still gets the pointer, so that silence "
              "is orientation and not a dead check",
              "first refusal in this session" in _refuse("sess-third"))
    finally:
        shutil.rmtree(ow, ignore_errors=True)

    print("the deploy refusal names the CHEAP right path, not only the rule:")
    # A guard staying correct depends partly on how expensive it is to work around at the moment
    # somebody is tired — an argument for making the RIGHT path cheap rather than for trusting
    # anyone's resolve, mine included. Measured on myself: this guard refused the commit that
    # documented what it refuses, because the commit text contained a deploy verb as a whole word.
    # The remedy took ten seconds and I only found it because I had used it that afternoon; the
    # refusal did not mention it. Ten seconds bought the right answer, an afternoon might not have.
    _dv = open(os.path.join(SRC_GAME_LOOP, "bin", "guard-writes-impl.sh")).read()
    check("the deploy refusal names the prose case and its remedy — writing ABOUT a verb is the "
          "commonest way to meet this guard, and the file route is cheap only if you know it",
          "WRITING ABOUT THE VERB" in _dv and "--body-file" in _dv and "-F <file>" in _dv)
    check("...and it states the trade it is making, where somebody MEETS it rather than only in "
          "the source: whole-word matching over command position, because missing a real publish "
          "is the expensive direction",
          "narrowing to command position" in _dv and "expensive direction" in _dv)

    print("a refusal names a hatch that exists (#52):")
    hp52 = make_sandbox()
    try:
        _gl52 = os.path.join(hp52, ".game_loop", "bin", "game_loop")

        def _reason(res):
            try:
                return json.loads(res.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            except Exception:  # noqa: BLE001
                return res.stdout

        _w = _reason(guard(hp52, {"tool_name": "Write",
                                  "tool_input": {"file_path": os.path.expanduser("~/evil52.txt")}}))
        _b = _reason(guard(hp52, {"tool_name": "Bash",
                                  "tool_input": {"command": "rm -rf ~/gone52"}}))
        _m = subprocess.run([os.path.join(hp52, ".game_loop", "bin", "guard-mcp.sh")],
                            input=json.dumps({"tool_name": "mcp__x__createThing",
                                              "tool_input": {"body": "b"}}),
                            capture_output=True, text=True, env=_env(hp52))
        _mr = _reason(_m)

        check("all three refusals name a PATH to the hatch, not a bare command that is on nobody's "
              "PATH",
              all("/bin/game_loop authorize" in r for r in (_w, _b, _mr)))
        # THE KEYSTONE, and the only one that could not be satisfied by rewording: the path printed
        # has to be a file that exists and runs. Extracted from the message and executed.
        _paths = []
        for r in (_w, _b, _mr):
            for tok in r.split():
                if tok.endswith("/bin/game_loop"):
                    _paths.append(tok)
                    break
        check("...and the path each one prints is a real executable — extracted from the message "
              "and run, rather than eyeballed",
              len(_paths) == 3
              and all(os.access(p, os.X_OK) for p in _paths)
              and all(subprocess.run([p, "authorize", "--help"], capture_output=True).returncode == 0
                      for p in _paths))
        check("...and it is the PROJECT's binary, so a pinned session authorises into the project's "
              "own record rather than the copy it happens to be running",
              all(os.path.realpath(p) == os.path.realpath(_gl52) for p in _paths))
        check("...and --uses is surfaced where it is needed, so a human who authorised a RUN of "
              "calls is asked once rather than once per call",
              all("--uses" in r for r in (_w, _b, _mr)))
    finally:
        shutil.rmtree(hp52, ignore_errors=True)

    print("a deploy verb is a WORD, not a substring (#51):")
    dp = make_sandbox()
    try:
        _dcfg = os.path.join(dp, ".game_loop", "config.json")
        with open(_dcfg) as f:
            _dc = json.load(f)
        _dc["deploy_verbs"] = ["surge"]          # the reporter's verb, so their repro is exercised
        with open(_dcfg, "w") as f:
            json.dump(_dc, f)
        _D, _P = "docker" + " push", "surge"

        def _dep(cmd):
            r = guard(dp, {"tool_name": "Bash", "cwd": dp, "tool_input": {"command": cmd}})
            return "deploy/publish verb" in r.stdout

        check("ordinary English containing a verb as a SUBSTRING is not a deploy — the reporter's "
              "own sentence, which their chat message and then their test both died on",
              not _dep("echo I do most file " + _P + "ry with a heredoc")
              and not _dep("echo the " + _D + "ed image was fine"))
        # THE PAIRED HALF. Without these, "stop matching substrings" is indistinguishable from
        # "stop matching", and missing a real publish is the expensive direction.
        check("...while a real deploy verb is still refused, alone and mid-chain",
              _dep(_D + " myimage:latest") and _dep("make build && " + _D))
        check("...and a configured verb is refused with its own arguments after it",
              _dep(_P + " --domain example.com"))
        check("...and one nested inside an interpreter argument still counts, since it executes "
              "just the same",
              _dep('bash -c "' + _D + ' x"'))
        # Already correct before the fix, and asserted so a later narrowing cannot silently lose it:
        # a leading word character means the verb was never at a boundary at all.
        check("a verb embedded mid-word was already not a match, and stays that way",
              not _dep("echo a re" + _P + "nce of interest")
              and not _dep("echo the in" + _P + "nt branch"))
    finally:
        shutil.rmtree(dp, ignore_errors=True)

    print("an unreadable commit target fails closed (#40):")
    up40 = make_sandbox()
    try:
        _CM = "commit"

        def _c(cmd):
            return guard(up40, {"tool_name": "Bash", "cwd": up40,
                                "tool_input": {"command": cmd}})

        def _unreadable(r):
            return denied(r) and "target tree is built from a variable" in r.stdout

        _var = "git -C \"$WT\" " + _CM + " -m x"
        check("a commit whose -C target is a variable is REFUSED, not passed as someone else's tree",
              _unreadable(_c(_var)))
        check("...and the refusal names the unreadable fragment, so it can be acted on",
              "$WT" in _c(_var).stdout)
        check("...and a cd into a variable poisons the commits that follow it, which is the shape "
              "fan-out actually generates",
              _unreadable(_c("cd \"$WT\" && git " + _CM + " -m x")))
        check("...and a command substitution counts as unreadable too, not only a $VAR",
              _unreadable(_c("git -C " + chr(96) + "pwd" + chr(96) + " " + _CM + " -m x")))

        # THE NARROWNESS CONTROLS. Without these, "fails closed" is indistinguishable from "refuses
        # anything with a dollar sign in it" — the version that gets a guard switched off (INV5).
        check("a NON-commit command carrying a variable is untouched: the trigger is a commit whose "
              "OWN target is unreadable, never any unparseable fragment",
              not denied(_c("ls \"$WT\" && echo hi")))
        check("...and prose ABOUT such a commit, inside a message flag, is not read as one",
              "target tree is built from a variable" not in
              _c("git " + _CM + " -m \"describes " + _CM + " -C $WT failing\"").stdout)
        check("...and --no-verify still opts out out loud, exactly as before",
              not _unreadable(_c("git -C \"$WT\" " + _CM + " --no-verify -m x")))
        check("...and an ordinary literal in-repo commit never sees this refusal",
              "target tree is built from a variable"
              not in _c("git " + _CM + " -m x").stdout)
        # $HOME is substituted before the check, so an ordinary home path stays RESOLVABLE — a rule
        # that refused those would be reworded around rather than obeyed.
        check("a $HOME path is resolvable and is not caught by the dynamic-target check",
              not _unreadable(_c("git -C \"$HOME/nope\" " + _CM + " -m x")))
    finally:
        shutil.rmtree(up40, ignore_errors=True)

    # PROSE THAT MENTIONS A REDIRECT IS NOT A REDIRECT (#46). An interpreter here-doc body is kept
    # in the scan on purpose — it executes — but it is PYTHON, not shell, so a comment inside it
    # mentioning `cat >` yielded a target with the backtick and following punctuation glued on. The
    # sink test then missed, and ordinary work was refused. This fired on the very edit that was
    # writing another guard's tests, and a false positive here has no escape but `authorize`, which
    # is single-use and logged: spending the one hatch on a non-event is the wrong use of it.
    print("a mentioned redirect is not a redirect (#46):")
    bt = chr(96)
    gp = make_sandbox()
    try:
        def _bash(cmd):
            return denied(guard(gp, {"tool_name": "Bash", "tool_input": {"command": cmd}}))

        check("a comment INSIDE an interpreter here-doc that mentions a redirect is not a write",
              not _bash("python3 - <<'PY'\n# ended " + bt + "else cat >/dev/null" + bt +
                        ", so it was silent\nprint(1)\nPY"))
        # The three arms that must NOT move, or the fix is a hole rather than a correction.
        check("...while a REAL redirect out of the repo is still refused",
              _bash("echo x > /var/tmp/gl46-real.txt"))
        check("...and a literal path with a substitution glued after it still refuses on the "
              "literal part",
              _bash("echo x > /var/tmp/gl46" + bt + "whoami" + bt))
        check("...and a discard sink is still not a write target at all",
              not _bash("echo hi >/dev/null") and not _bash("echo hi 2>/dev/null"))
        # STATED, NOT FIXED. A target that STARTS with a substitution is captured up to the space
        # inside it, resolves INSIDE the repo, and is allowed — a pre-existing hole of the same
        # family as #40 (a target the guard cannot resolve escapes it). Asserted here so the #46
        # fix is proved not to have moved it, and so the hole is recorded rather than assumed gone.
        check("a target that STARTS with a substitution is still allowed — pre-existing, #40's "
              "family, and pinned here so this fix is shown not to have touched it",
              not _bash("echo x > " + bt + "echo /var/tmp/gl46-sub.txt" + bt))
    finally:
        shutil.rmtree(gp, ignore_errors=True)

    # LINKED WORKTREES ARE THIS PROJECT (#47). Tree identity resolved from the BINARY's location, so
    # an agent working in a sibling worktree — invoking a main checkout's game_loop by absolute path,
    # which is what an unprovisioned worktree does — had its writes denied as "outside this repo".
    # One reported session bought TWELVE single-use authorizations to write the files it was spawned
    # to change. A gate that fires on all normal work gets routed around rather than respected, so
    # this is a correctness fix and not a convenience.
    #
    # Fixtures live under /var/tmp ON PURPOSE. It is writable and it is the one temp location NOT on
    # the guard's allow list (/tmp, /private/tmp and /var/folders all are), so a worktree there has
    # to be allowed BY BEING A WORKTREE rather than by sitting in a temp dir. Anywhere else the
    # assertion would pass without the fix and prove nothing.
    print("a linked worktree is the same project (#47):")
    wtbase = os.path.join("/var/tmp", f"gl47-{os.getpid()}")
    try:
        def _mkproj(path):
            os.makedirs(os.path.join(path, ".game_loop", "bin"))
            for f in ("game_loop", "guard-writes.sh", "guard-writes-impl.sh", "guard-mcp.sh",
                      "guard-mcp-impl.sh", "watchdog", "verify", "flair.py", "notify.py"):
                shutil.copy(os.path.join(SRC_GAME_LOOP, "bin", f),
                            os.path.join(path, ".game_loop", "bin", f))
                os.chmod(os.path.join(path, ".game_loop", "bin", f), 0o755)
            for f in ("config.json", "verify.yaml", "INVARIANTS.md"):
                shutil.copy(os.path.join(SRC_GAME_LOOP, f), os.path.join(path, ".game_loop", f))
            for a in (["init", "-q", path], ["-C", path, "config", "user.email", "t@t"],
                      ["-C", path, "config", "user.name", "t"]):
                subprocess.run(["git", *a], capture_output=True)
            with open(os.path.join(path, "seed.txt"), "w") as f:
                f.write("x\n")
            subprocess.run(["git", "-C", path, "add", "-A"], capture_output=True)
            subprocess.run(["git", "-C", path, "commit", "-qm", "seed"], capture_output=True)
            return path

        os.makedirs(wtbase)
        wproj = _mkproj(os.path.join(wtbase, "proj"))
        wtree = os.path.join(wtbase, "wt")
        subprocess.run(["git", "-C", wproj, "worktree", "add", "-q", wtree], capture_output=True)
        wother = _mkproj(os.path.join(wtbase, "other"))     # a DIFFERENT repo, same parent dir

        def _v(payload):
            return denied(guard(wproj, payload))

        check("a worktree fixture was really created (else every verdict below is vacuous)",
              os.path.isdir(wtree) and os.path.isfile(os.path.join(wtree, ".git")))
        check("a write into a LINKED WORKTREE is allowed — it is this project, checked out twice",
              not _v({"tool_name": "Write",
                      "tool_input": {"file_path": os.path.join(wtree, "a.txt")}}))
        # THE NARROWNESS CONTROL. Without this, "allow the worktree" is indistinguishable from
        # "allow anything next door", and the guard would have been widened into uselessness.
        check("...while a DIFFERENT repository in the same parent dir is still denied",
              _v({"tool_name": "Write",
                  "tool_input": {"file_path": os.path.join(wother, "a.txt")}}))
        check("...and a loose out-of-repo path is still denied",
              _v({"tool_name": "Write",
                  "tool_input": {"file_path": os.path.join(wtbase, "loose.txt")}}))
        check("the same holds for a mutating BASH command aimed at the worktree",
              not _v({"tool_name": "Bash", "tool_input": {"command": f"touch {wtree}/b.txt"}}))
        check("...and for one aimed at the other repository, which stays denied",
              _v({"tool_name": "Bash", "tool_input": {"command": f"touch {wother}/b.txt"}}))
        check("the two guard paths agree — Write and Bash are separate interpreters carrying their "
              "own copy of the rule, so they are asserted against the same pair of trees",
              not _v({"tool_name": "Write", "tool_input": {"file_path": os.path.join(wtree, "c")}})
              and not _v({"tool_name": "Bash", "tool_input": {"command": f"touch {wtree}/d"}}))
        check("status says the write rail reaches linked worktrees, so the widening is not silent",
              "LINKED WORKTREES" in gl(wproj, "status").stdout)

        # THE MISREPORT HALF. The harm was never that it answered wrongly; it was that it answered
        # CONFIDENTLY about a tree the agent was not in, with nothing anywhere saying so.
        def _status_from(cwd):
            return subprocess.run([os.path.join(wproj, ".game_loop", "bin", "game_loop"), "status"],
                                  capture_output=True, text=True, cwd=cwd,
                                  env=_env(wproj, sid="sess-wt")).stdout

        check("run from its OWN tree the harness says nothing — this is every normal invocation",
              "NOT IN THE TREE" not in _status_from(wproj))
        _from_wt = _status_from(wtree)
        check("run from a linked worktree it SAYS the tree it answers for is a different one",
              "NOT IN THE TREE" in _from_wt and "LINKED WORKTREE of this same project" in _from_wt)
        check("...naming BOTH trees, since a warning you cannot act on is just noise",
              wtree in _from_wt and wproj in _from_wt and "install.sh" in _from_wt)
        _from_other = _status_from(wother)
        check("...and it tells a different REPOSITORY apart from a worktree of this one — the two "
              "have different remedies",
              "DIFFERENT repository" in _from_other)
    finally:
        subprocess.run(["git", "-C", os.path.join(wtbase, "proj"), "worktree", "remove",
                        "--force", os.path.join(wtbase, "wt")], capture_output=True)
        shutil.rmtree(wtbase, ignore_errors=True)

    # TRIGGERS. Some things worth doing at a moment in the loop cannot ship as a rule, because
    # they need infrastructure most installs do not have — the one that prompted this is a shared
    # channel other agents read. So the harness owns the MOMENT, and the project owns what happens
    # there.
    #
    # The failure mode this design is most exposed to is the one the usage-limit tap just taught:
    # something configured, never running, and looking exactly like something working. So every
    # assertion here is paired, and `status` is required to name an attachment that has never fired.
    print("a moment nobody can find is a moment nobody attaches to:")
    # The CODE is the list of moments. The example config is where an integrator actually looks and
    # the README table is where a reader first learns they exist, so both are checked AGAINST the
    # code rather than against a hand-copied list — publishing a fifth moment and forgetting the
    # docs then fails here, instead of being discovered by whoever needed it. #63 is the case: the
    # moment already existed inside `sessionstart` and was reachable from nothing anybody reads.
    # WHAT THIS DOES NOT CHECK (INV6): that the prose is right, or explains anything. A name
    # appearing is the floor, not the goal, and no grep can hold the rest.
    _gl_ast = ast.parse(open(os.path.join(SRC_GAME_LOOP, "bin", "game_loop")).read())
    _moments = []
    for _n in ast.walk(_gl_ast):
        if isinstance(_n, ast.Assign) and any(getattr(t, "id", "") == "TRIGGER_EVENTS"
                                              for t in _n.targets):
            _moments = [k.value for k in _n.value.keys]
    _ex_src = open(os.path.join(REPO, "templates", "triggers.example.json")).read()
    _rm_src = open(os.path.join(REPO, "README.md")).read()
    # A KEY IN THE EXAMPLE, not a substring of it. `stop` (#64) is the case that made the substring
    # test worthless: it occurs inside "stopped", "stop gate" and a dozen sentences, so a moment
    # published and documented nowhere would have passed on the strength of prose about something
    # else. A parsed key cannot be satisfied by an accident of wording.
    _ex_keys = set(json.loads(_ex_src))

    def _undocumented(names):
        return [m for m in names if m not in _ex_keys or f"`{m}`" not in _rm_src]

    check(f"every moment the code publishes is named in the example config AND the README table "
          f"({len(_moments)} published)"
          + (" · MISSING: " + ", ".join(_undocumented(_moments)) if _undocumented(_moments) else ""),
          len(_moments) >= 4 and not _undocumented(_moments))
    check("...and the check FIRES on a name neither file carries, so a green result above is a "
          "comparison rather than an empty loop agreeing with itself",
          _undocumented(["a-moment-nobody-wrote-down"]) == ["a-moment-nobody-wrote-down"])

    print("every configured trigger command actually exists and can run:")
    # NAMES NO TOOL, deliberately. It reads whatever is configured, so it cannot walk back into the
    # thing it is guarding against — a check that hardcoded a neighbour's path would put that
    # neighbour's name in a public tree, which is how this started.
    _tj = os.path.join(SRC_GAME_LOOP, "triggers.json")
    if not os.path.exists(_tj):
        # SKIPPED, NOT FAILED, and said out loud. triggers.json is gitignored and a fresh clone
        # legitimately has none; failing here would make every consumer's suite red for a file they
        # were never meant to have.
        check("no triggers.json in this checkout — nothing configured to check (skipped, not "
              "passed: a clone legitimately has none)", True)
    else:
        with open(_tj) as f:
            _tcfg = json.load(f)
        _cmds = [(ev, t.get("name"), t.get("command") or "")
                 for ev, ts in _tcfg.items() if isinstance(ts, list) for t in ts]

        def _trigger_target(cmd):
            """The file a trigger command runs, with $GAME_LOOP_ROOT expanded — or None."""
            c = cmd.replace("$GAME_LOOP_ROOT", SRC_GAME_LOOP).replace(
                "${GAME_LOOP_ROOT}", SRC_GAME_LOOP)
            for tok in shlex.split(c):
                if os.path.sep in tok and not tok.startswith("-"):
                    return tok
            return None

        _broken = []
        for _ev, _name, _cmd in _cmds:
            _t = _trigger_target(_cmd)
            if _t is None:
                continue                      # an inline command with no file is its own business
            if not (os.path.isfile(_t) and os.access(_t, os.R_OK)):
                _broken.append(f"{_name}: {_t}")
        check(f"every configured trigger names a file that exists and is readable "
              f"({len(_cmds)} configured)"
              + (" · BROKEN: " + "; ".join(_broken[:3]) if _broken else ""),
              not _broken)
        # PAIRED, because "it passed" must not be reachable by having looked at nothing — the exact
        # silence this check exists to prevent, reproduced inside the check itself.
        check("...and it EXAMINED something, so a green result is a verdict rather than an empty "
              "loop over a config with no commands in it",
              len(_cmds) > 0)
        check("...and the check can FIRE — a command naming a path that does not exist is reported",
              _trigger_target('bash "/definitely/not/here/nope.sh"') == "/definitely/not/here/nope.sh"
              and not os.path.isfile("/definitely/not/here/nope.sh"))

    print("a trigger wired to a moment that does not exist can never fire, and says so:")
    # THE HALF ONLY THIS REPO CAN FALSIFY. A neighbouring project hosts a trigger and asserts that
    # its own docstring, wiring example and instruction all AGREE about which moment to recommend.
    # Agreement is all it can check: if it recommended a fourth moment that does not exist, all
    # three would say so in unison and its suite would pass. Whether the moment EXISTS is a fact
    # about game_loop, falsifiable only here — its own words, handed over as the residue.
    gw = make_sandbox()
    try:
        _gtj = os.path.join(gw, ".game_loop", "triggers.json")
        with open(_gtj, "w") as f:
            json.dump({"blessing": [{"name": "ghost", "command": "echo hi"}],
                       "harden": [{"name": "real", "command": "echo hi"}]}, f)
        _gs = gl(gw, "status", sid="sess-ghost").stdout
        check("a trigger wired to a moment game_loop does not have is told it will NEVER fire — "
              "not that it might be waiting, which is what 'never fired' reads as",
              "THERE IS NO SUCH MOMENT" in _gs and "never fire" in _gs)
        check("...and the refusal LISTS the real moments, so the reader can fix it from the "
              "message rather than going to look for them",
              "confidence" in _gs and "harden" in _gs and "stepback" in _gs)
        check("...and the list is the CODE's list, so a moment added later is discoverable from the "
              "refusal itself rather than only from whichever doc the reader happened to open",
              any("game_loop's moments are" in ln and "session_start" in ln
                  for ln in _gs.splitlines()))
        # PAIRED, or the check is just a louder version of the ambiguity it replaced: a REAL moment
        # that simply has not come round yet must still be reported as patient, not as broken.
        check("...while a real moment that has merely not come round yet is still reported as "
              "possibly waiting — two causes that used to share one message now have two",
              "CONFIGURED BUT NEVER FIRED" in _gs)
    finally:
        shutil.rmtree(gw, ignore_errors=True)

    print("triggers — a project's own attachments to the loop:")
    tpp = make_sandbox()
    try:
        _tpf = os.path.join(tpp, ".game_loop", "triggers.json")
        _cfgf = os.path.join(tpp, ".game_loop", "config.json")

        def _tp_write(cfg):
            with open(_tpf, "w") as f:
                json.dump(cfg, f)

        def _tp_cfg(**kw):
            with open(_cfgf) as f:
                c = json.load(f)
            c.update(kw)
            with open(_cfgf, "w") as f:
                json.dump(c, f)

        def _run(*args, sid="sess-tp"):
            return subprocess.run([os.path.join(tpp, ".game_loop", "bin", "game_loop"), *args],
                                  capture_output=True, text=True, cwd=tpp, env=_env(tpp, sid=sid))

        def _harden(*extra, sid="sess-tp"):
            return _run("harden", "--learning", "L", "--artifact", ".game_loop/config.json",
                        "--mechanism", "M", "--rung", "3", *extra, sid=sid)

        # PAIRED FIRST: with nothing attached the loop must not mention triggers at all. Most
        # installs want no part of this, and a feature that narrates its own absence is noise.
        _none = _harden("--general", "G")
        check("nothing attached → harden says nothing about triggers",
              "triggers ·" not in _none.stdout and "HARDENED" in _none.stdout)
        check("...and status stays silent about them too",
              "TRIGGERS" not in _run("status").stdout)

        _tp_write({"harden": [{"name": "tp-ok", "command": "cat; echo FIRED-OK"}]})
        _fired = _harden("--general", "the transferable form")
        check("an attached trigger RUNS at its moment, and its stdout comes back to the agent",
              "FIRED-OK" in _fired.stdout and "triggers · harden" in _fired.stdout)
        check("...and the payload arrives as JSON on stdin, carrying the GENERAL form",
              '"general": "the transferable form"' in _fired.stdout
              and '"event": "harden"' in _fired.stdout)

        # A trigger must never be able to veto the work it is only reporting on (INV5).
        _tp_write({"harden": [{"name": "tp-bad", "command": "echo nope >&2; exit 3"}]})
        _bad = _harden("--general", "G")
        check("a FAILING trigger is announced loudly, naming the failure",
              "tp-bad FAILED" in _bad.stdout and "nope" in _bad.stdout)
        check("...and the harden still stands — a trigger never blocks the work",
              _bad.returncode == 0 and "HARDENED" in _bad.stdout
              and "never blocks the work" in _bad.stdout)

        _tp_write({"harden": [{"name": "tp-slow", "command": "sleep 5", "timeout_sec": 1}]})
        _slow = _harden("--general", "G")
        check("a trigger that hangs is bounded and SAYS it timed out",
              "tp-slow FAILED" in _slow.stdout and "timed out" in _slow.stdout
              and "HARDENED" in _slow.stdout)

        # GENERALISING is a separate act from hardening: the incident form rarely transfers. Nudge
        # only where somebody has actually attached a sharing step, and never block.
        _tp_write({"harden": [{"name": "tp-ok", "command": "cat; echo FIRED-OK"}]})
        _nogen = _harden()
        check("a harden with no --general is nudged WHERE a sharing trigger is attached",
              "no --general" in _nogen.stdout and "HARDENED" in _nogen.stdout)
        check("...and giving one silences the nudge — so the nudge is about the FORM, not the flag",
              "no --general" not in _harden("--general", "G").stdout)
        _tp_write({})
        check("...and with nothing attached there is no nudge at all: nobody is asked to share into "
              "a void",
              "no --general" not in _harden().stdout)

        # THE VISIBILITY REQUIREMENT. Attached and working are different claims.
        _tp_write({"stepback": [{"name": "tp-never", "command": "echo x"}]})
        _st = _run("status").stdout
        check("a trigger that has NEVER fired is named as such in status",
              "tp-never" in _st and "CONFIGURED BUT NEVER FIRED" in _st)
        check("...and status names the moments nothing is attached to, so an unused one is a "
              "CHOICE rather than an oversight",
              # matched on the LINE, not a fixed neighbour: adding a moment reorders the list, and
              # an assertion pinned to "harden" being first fails for a reason that is not a defect.
              any("nothing attached" in ln and "harden" in ln for ln in _st.splitlines()))
        _run("stepback", "--notes", "n")
        _st2 = _run("status").stdout
        check("...and once it has fired, status shows it as fired instead — the pair that makes the "
              "warning above mean something",
              "tp-never" in _st2 and "CONFIGURED BUT NEVER FIRED" not in _st2)

        # A trigger that fires and FAILS every time is the third state, and the one most likely to
        # go unnoticed: it has fired, so the never-fired warning is silent about it.
        _tp_write({"harden": [{"name": "tp-broken", "command": "exit 4"}]})
        _harden("--general", "G")
        _st3 = _run("status").stdout
        check("a trigger that fires but FAILS is carried into status as failing, not as fired",
              "tp-broken" in _st3 and "FAILING" in _st3)

        # THE RETRO NUDGE, which could not fire for the whole life of this project: it counted
        # `trans`, an optional verb that had run ONCE against twelve hardens and zero retros.
        _tp_write({})
        _tp_cfg(work_nudge_every=2, trans_nudge_every=99)
        _run("stepback", "--notes", "reset the counters")
        _harden(sid="sess-nudge")
        check("evidence work below the threshold does NOT nudge — the control for the next one",
              "since the last retro" not in _run("status", sid="sess-nudge").stdout)
        _harden(sid="sess-nudge")
        _nudged = _run("status", sid="sess-nudge").stdout
        check("work that LOGS ITSELF reaches the threshold and the retro nudge fires — with zero "
              "transitions, which is the arrangement that was silent forever",
              "since the last retro" in _nudged and "stepback" in _nudged)

        # A retro that produces nothing is indistinguishable afterwards from one that never happened.
        _yield = _run("stepback", "--notes", "n", sid="sess-nudge").stdout
        check("a retro opens by reporting what the LAST one yielded, counted from the log",
              "WHAT THE LAST RETRO YIELDED" in _yield and "2 hardened" in _yield)
        _empty = _run("stepback", "--notes", "n", sid="sess-nudge").stdout
        check("...and a retro that encoded nothing is SAID to have encoded nothing",
              "NOTHING WAS HARDENED" in _empty)
        # The yield is the whole ledger, not just hardens — a chapter can be all evidence and no
        # encoding, and the report has to be able to show that shape rather than one number.
        _run("claim", "--assert", "a", "--read", ".game_loop/config.json", sid="sess-nudge")
        _tp_write({"harden": [{"name": "tp-y", "command": "echo y"}]})
        _harden("--general", "G", sid="sess-nudge")
        _ledger = _run("stepback", "--notes", "n", sid="sess-nudge").stdout
        check("the yield counts claims and triggers fired too, not hardens alone",
              "1 claims" in _ledger and "1 triggers fired" in _ledger and "1 hardened" in _ledger)
        check("...while the one that did encode something is not accused of it — the pair that "
              "keeps the warning honest",
              "NOTHING WAS HARDENED" not in _yield)

        # Others' learnings must arrive BEFORE the reflection, or they are an appendix to thinking
        # that has already happened.
        _tp_write({"stepback": [{"name": "tp-in", "command": "echo INCOMING-LEARNING"}]})
        _sb = _run("stepback", "--notes", "n", sid="sess-nudge").stdout
        check("a stepback trigger's output lands BEFORE the retro's own output, not after",
              "INCOMING-LEARNING" in _sb
              and _sb.index("INCOMING-LEARNING") < _sb.index("STEP-BACK — invariants re-injected"))
    finally:
        shutil.rmtree(tpp, ignore_errors=True)

    # ── THE MOMENT THAT DECIDES INSTEAD OF ANNOUNCING (#64) ─────────────────────────────────────
    # Every trigger above is an announcement whose exit code changes nothing — "a trigger never
    # blocks the work" is printed under each failure. This one blocks: non-zero refuses turn-end and
    # its stderr goes back to the model. It exists because "reply when a human addresses you" lived
    # in prose, and an agent asked a direct question through a chat bridge did the work and ended
    # its turn without answering it.
    #
    # Blocking is a far sharper capability than announcing, and it is what the assertions here are
    # mostly about: every arm that says "it does NOT block" is paired with one showing it DOES, or
    # the arm is equally satisfied by a mechanism that never runs anything at all.
    print("the stop moment — a project's own check can REFUSE turn-end:")
    stp = make_sandbox()
    try:
        _stf = os.path.join(stp, ".game_loop", "triggers.json")
        _seen = os.path.join(stp, "stop-trigger-ran")
        _pay = os.path.join(stp, "stop-payload-seen.json")

        def _attach_stop(*trigs):
            with open(_stf, "w") as f:
                json.dump({"stop": list(trigs)}, f)

        def _end(sid="sess-stop", msg="Done. Remaining: nothing.", **extra):
            """One turn-end, driven through the real Stop hook entry point."""
            return gl(stp, "stopgate", sid=sid,
                      stdin=json.dumps({"session_id": sid, "last_assistant_message": msg, **extra}))

        # THE CONTROL FIRST. An install with nothing attached must be untouched — a moment that
        # narrates its own absence on every turn-end is a tax on everybody who never asked for it.
        check("nothing attached to `stop` → turn-end is exactly as it was, and silent about it",
              _end().returncode == 0 and not _end().stderr.strip())

        _attach_stop({"name": "answer-owed",
                      "command": "echo 'you owe @jonah a reply in #dev' >&2; exit 1"})
        _blocked = _end()
        check("an attachment that exits NON-ZERO BLOCKS turn-end (exit 2) — the contract the "
              "mandate gate already uses, handed to a command this repo did not write",
              _blocked.returncode == 2 and "STOP GATE CLOSED" in _blocked.stderr)
        check("...and ITS OWN stderr is what goes back to the model, so the reason belongs to the "
              "project rather than being a generic refusal the agent has to go and interpret",
              "you owe @jonah a reply in #dev" in _blocked.stderr
              and "answer-owed" in _blocked.stderr)
        check("...and the message names the bound out loud, so the agent is not left believing it "
              "has hit a wall that never comes down",
              "consecutive block 1 of 3" in _blocked.stderr
              and "stands down after 3" in _blocked.stderr)

        # THE PAIR THAT MAKES ALL OF IT MEAN SOMETHING: the same attachment, saying yes.
        _attach_stop({"name": "answer-owed", "command": "exit 0"})
        _ok = _end()
        check("...while the same attachment exiting ZERO lets the turn end, silently — the block "
              "above was a VERDICT, not the mere presence of a `stop` key",
              _ok.returncode == 0 and not _ok.stderr.strip())

        # The payload, checked by what the command RECEIVED rather than by what the harness says it
        # sends. An attachment deciding whether a turn may end needs the closing message above all:
        # "did it answer the question" is a question about that text.
        _attach_stop({"name": "reads-payload",
                      "command": f"cat > {shlex.quote(_pay)}; exit 0"})
        _end(msg="I have finished the migration.")
        try:
            # DEFENSIVELY, because the file is the assertion. An attachment that never ran writes
            # nothing, and letting that raise here would take the whole suite down with it — every
            # assertion after this point would die for a reason having nothing to do with them,
            # which is how a mutation run reports coverage it does not have.
            with open(_pay) as f:
                _seen_payload = json.load(f)
        except (OSError, ValueError):
            _seen_payload = {}
        check("the payload carries the agent's CLOSING MESSAGE and names the moment, so an "
              "attachment can decide about this turn rather than about the world in general",
              _seen_payload.get("last_assistant_message") == "I have finished the migration."
              and _seen_payload.get("event") == "stop"
              and _seen_payload.get("session") == "sess-stop")

        # ── FAIL OPEN, AND SAY SO ───────────────────────────────────────────────────────────────
        # A trigger that ERRORS must not wedge the session: a guard must never block its own fix
        # (INV5), and an attachment that hangs while somebody is repairing it would otherwise hold
        # the session shut with the repair inside it. Both arms are driven by a command that
        # genuinely errors, never by one asserted to have errored.
        _attach_stop({"name": "hangs", "command": "sleep 30", "timeout_sec": 1})
        _t0 = time.time()
        _to = _end()
        check("an attachment that HANGS is bounded, fails OPEN, and the turn ends",
              _to.returncode == 0 and time.time() - _t0 < 20)
        check("...and it is LOUD about it, and refuses to be read as a pass — 'nothing was decided' "
              "and 'it said yes' are the two states this harness exists to keep apart",
              "FAILED OPEN" in _to.stderr and "UNCHECKED" in _to.stderr
              and "timed out" in _to.stderr and "not a pass" in _to.stderr)
        # A DIFFERENT genuine error, and the sharper one: a `command` that is not a string raises
        # from inside subprocess rather than returning an exit code. That used to propagate out of
        # the trigger runner, whose docstring promised it raises nothing — one badly typed line of
        # site config, and the Stop hook itself would crash on every turn-end.
        _attach_stop({"name": "typed-wrong", "command": 1234})
        _crash = _end()
        check("an attachment the harness cannot even RUN is an answer, not a crash: the turn ends "
              "and the notice names it",
              _crash.returncode == 0 and "FAILED OPEN" in _crash.stderr
              and "typed-wrong" in _crash.stderr and "could not run" in _crash.stderr)

        # ── THE BOUND, WHICH IS NOT THE SAME HAZARD AS THE FAIL-OPEN ────────────────────────────
        # An attachment can run perfectly and return a perfectly correct "still owed" that nobody
        # present can clear — the room it asks about is down. That is neither a crash nor a timeout,
        # so failing open on those does not touch it, and unbounded it is a gate no session in the
        # tree could ever pass: the harness itself preventing every agent from finishing.
        _attach_stop({"name": "impossible", "command": "echo 'the room is unreachable' >&2; exit 1"})
        _runs = [_end(sid="sess-bound") for _ in range(4)]
        check("three consecutive blocks all hold — the bound is not a way of blocking once and "
              "giving up, which would make the whole moment advisory",
              [r.returncode for r in _runs[:3]] == [2, 2, 2]
              and "consecutive block 3 of 3" in _runs[2].stderr)
        check("...and the FOURTH stands down: the turn ends even though the attachment is still "
              "saying no, because a block nobody present can clear would gate every turn forever",
              _runs[3].returncode == 0 and "STOOD DOWN" in _runs[3].stderr)
        check("...and the notice NAMES the attachment, the count, and what it last said — a gate "
              "that quietly stops holding is worse than one that never held",
              "impossible" in _runs[3].stderr and "4 consecutive turn-ends" in _runs[3].stderr
              and "the room is unreachable" in _runs[3].stderr)
        # THE RESET, and it is the half that keeps the bound from being a one-way door: an
        # attachment that stood down must be able to come back, or the first bad afternoon disarms
        # it permanently and nothing says so.
        _attach_stop({"name": "impossible", "command": "exit 0"})
        _end(sid="sess-bound")
        _attach_stop({"name": "impossible", "command": "echo 'owed again' >&2; exit 1"})
        _again = _end(sid="sess-bound")
        check("one PASS resets the count and puts the attachment back in charge — a stand-down is "
              "the harness declining to honour a block, never a permanent disarm",
              _again.returncode == 2 and "consecutive block 1 of 3" in _again.stderr)
        # And the streak is reset ONLY by a pass. Letting "could not tell" clear it hands back the
        # unbounded case through the side door: an attachment that fails every other turn would
        # block half of them forever and never reach its bound.
        _attach_stop({"name": "impossible", "command": "sleep 30", "timeout_sec": 1})
        _mid = _end(sid="sess-bound")
        _attach_stop({"name": "impossible", "command": "echo 'still owed' >&2; exit 1"})
        _after = _end(sid="sess-bound")
        check("an ERROR mid-streak neither counts as a block nor clears the run of them — the "
              "streak resumes where it was, so a trigger that errors every other turn still "
              "reaches its bound",
              _mid.returncode == 0 and "FAILED OPEN" in _mid.stderr
              and _after.returncode == 2 and "consecutive block 2 of 3" in _after.stderr)

        # ── WHAT A BLOCK MUST NOT COST ──────────────────────────────────────────────────────────
        # A checkpoint, an arm and a park are each single-use. Spending one on a turn-end that then
        # gets blocked would burn the human's interruption on a turn that never ended.
        _attach_stop({"name": "owed", "command": "echo nope >&2; exit 1"})
        gl(stp, "mandate", "--set", "do the work", sid="sess-keep")
        gl(stp, "checkpoint", "--notes", "reporting", sid="sess-keep")
        _kb = _end(sid="sess-keep")
        _attach_stop({"name": "owed", "command": "exit 0"})
        _kept = _end(sid="sess-keep")
        check("a trigger block does not CONSUME the checkpoint that turn-end had declared — the "
              "same checkpoint still carries the next one, so clearing the trigger costs one turn "
              "and nothing else",
              _kb.returncode == 2 and _kept.returncode == 0)
        check("...and the pair that proves it: with the checkpoint now spent, the next bare "
              "turn-end is refused by the mandate gate again",
              _end(sid="sess-keep").returncode == 2)

        # It runs only where turn-end would otherwise be ALLOWED. A turn the mandate gate has
        # already refused is not a turn-end, so the attachment's question is moot — and asking it
        # anyway would spend its timeout on every refused turn and count a block against a bound
        # that is measured in turn-ends.
        _attach_stop({"name": "marks", "command": f"date >> {shlex.quote(_seen)}; exit 0"})
        gl(stp, "mandate", "--set", "do the work", sid="sess-order")
        _asked = _end(sid="sess-order", msg="Which colour do you want?")
        check("a turn-end the mandate gate REFUSES never reaches the attachment — proved by a file "
              "that does not appear, not by an absence of output",
              _asked.returncode == 2 and not os.path.exists(_seen))
        gl(stp, "checkpoint", "--notes", "reporting", sid="sess-order")
        _allowed = _end(sid="sess-order")
        check("...and on a turn-end it ALLOWS, the same attachment runs — without this the line "
              "above is equally satisfied by an attachment that never runs at all",
              _allowed.returncode == 0 and os.path.exists(_seen))

        # THE MANDATE GATE'S CIRCUIT BREAKER IS ITS OWN. Two blocks stand THAT gate down; a trigger
        # block must not spend that budget, or one gate disarms another for a reason that has
        # nothing to do with it.
        _attach_stop({"name": "owed", "command": "echo nope >&2; exit 1"})
        gl(stp, "mandate", "--set", "do the work", sid="sess-breaker")
        gl(stp, "checkpoint", "--notes", "r1", sid="sess-breaker")
        _tb1, _tb2 = _end(sid="sess-breaker"), _end(sid="sess-breaker")
        _attach_stop({"name": "owed", "command": "exit 0"})
        _spend = _end(sid="sess-breaker")   # only NOW is that checkpoint consumed
        _bare = _end(sid="sess-breaker")
        check("two trigger blocks do NOT stand the mandate gate down — its breaker counts ITS own "
              "blocks, and a bare turn-end after them is still refused",
              [_tb1.returncode, _tb2.returncode, _spend.returncode] == [2, 2, 0]
              and _bare.returncode == 2 and "no checkpoint declared" in _bare.stderr)
        _end(sid="sess-breaker")           # the mandate gate's second block, on its own account
        check("...while two of the mandate gate's OWN blocks do trip it, which is the pair showing "
              "the breaker still works rather than having been disabled by the line above",
              _end(sid="sess-breaker").returncode == 0)

        # SEVERAL ATTACHMENTS: all of them run even after one has said no. The agent gets ONE
        # turn-end and should be told everything it owes, rather than discovering the second
        # obligation only after clearing the first — N attachments would otherwise cost N turns.
        _attach_stop({"name": "first", "command": "echo FIRST-SAYS-NO >&2; exit 1"},
                     {"name": "second", "command": "echo SECOND-SAYS-NO >&2; exit 1"})
        _both = _end(sid="sess-many")
        check("a second attachment still runs after the first has blocked, and both reasons arrive "
              "in one refusal",
              _both.returncode == 2 and "FIRST-SAYS-NO" in _both.stderr
              and "SECOND-SAYS-NO" in _both.stderr)
        check("...and each carries its OWN count, since the bound is a statement about one "
              "attachment: two blocking once is not one blocking twice",
              _both.stderr.count("consecutive block 1 of 3") == 2)

        # COUNTABLE. "Why did this not stop" has to stay answerable, and a trigger-sourced block is
        # a block: it lands in the same tally as the mandate gate's.
        def _gate_count(sid):
            m = re.search(r"gate (\d+)\)", gl(stp, "status", sid=sid).stdout)
            return int(m.group(1)) if m else 0

        _before = _gate_count("sess-count")
        _attach_stop({"name": "owed", "command": "exit 1"})
        _end(sid="sess-count")
        _mid_count = _gate_count("sess-count")
        _attach_stop({"name": "owed", "command": "exit 0"})
        _end(sid="sess-count")
        check("a trigger-sourced block is COUNTED like every other block this gate issues, so the "
              "tally still answers how often turn-end was held",
              _mid_count == _before + 1)
        check("...and a turn the attachment PASSED is not counted as one — the pair that keeps the "
              "number a count of refusals rather than of turn-ends",
              _gate_count("sess-count") == _mid_count)

        # THREE OUTCOMES, THREE READINGS, in `status` as well as in the refusal. The next session
        # reads status and nothing else; a stood-down attachment that reads like a working one is
        # the silence this whole project is about.
        _attach_stop({"name": "reported", "command": "echo owed >&2; exit 1"})
        _end(sid="sess-report")
        _st_block = gl(stp, "status", sid="sess-report").stdout
        check("status says an attachment is BLOCKING turn-end, and how close it is to standing "
              "down — 'FAILING' alone would read as harmless",
              "BLOCKING turn-end — 1 consecutive of 3" in _st_block)
        for _ in range(3):
            _end(sid="sess-report")
        _st_down = gl(stp, "status", sid="sess-report").stdout
        check("...and once it has stood down, status says THAT instead — a gate that has quietly "
              "stopped holding is exactly what a status block exists to prevent",
              "STOOD DOWN" in _st_down and "OVERRIDDEN" in _st_down
              and "BLOCKING turn-end" not in _st_down)
        _attach_stop({"name": "reported", "command": "sleep 30", "timeout_sec": 1})
        _end(sid="sess-report")
        _st_open = gl(stp, "status", sid="sess-report").stdout
        check("...and an attachment that could not ANSWER reads as a third thing again: the last "
              "turn-end went unchecked, which is neither a block nor a pass",
              "FAILED OPEN" in _st_open and "UNCHECKED" in _st_open
              and "STOOD DOWN" not in _st_open)

        # It runs under stop_hook_active, where the mandate gate declines to stack a second block.
        # That gate can afford to defer because what it asks for is satisfiable in this session;
        # deferring here would make the check vacuous — the attachment would fire once per
        # continuation and never verify that what it asked for actually happened.
        _attach_stop({"name": "owed", "command": "echo still-owed >&2; exit 1"})
        gl(stp, "mandate", "--set", "do the work", sid="sess-reentry")
        _re = _end(sid="sess-reentry", stop_hook_active=True)
        check("a re-entered turn-end still consults the attachment — deferring there would make "
              "the check advisory, and the consecutive bound is what makes it safe not to",
              _re.returncode == 2 and "still-owed" in _re.stderr)
    finally:
        shutil.rmtree(stp, ignore_errors=True)

    # PINNED HARNESS. game_loop dogfoods itself: the hooks guarding a session run the very bin/ that
    # session is editing, so a half-finished gate is live in the same breath it is written. A merge
    # left conflict markers in bin/game_loop and every verb died with a SyntaxError; a shell parse
    # error in the write guard once blocked every tool call including its own fix. Running the CODE
    # from a pinned checkout fixes that — and the NAIVE version of it switches dogfooding off in
    # silence, because bin/verify resolves the tree it checks from its own __file__ and a pinned copy
    # resolves that to ITSELF. So GAME_LOOP_HOME splits the two: CODE pinned, HOME (rules, state,
    # log, pins) in the repo. These tests build the trap exactly and prove it does not happen.
    print("pinned harness (GAME_LOOP_HOME):")
    sp = make_sandbox()
    pin = tempfile.mkdtemp(prefix="gameloop-pin-")
    try:
        def spgit(*args):
            return subprocess.run(["git", "-c", "user.email=t@example.invalid",
                                   "-c", "user.name=tester", "-c", "commit.gpgsign=false", *args],
                                  cwd=sp, capture_output=True, text=True)

        home = os.path.join(sp, ".game_loop")
        # A project that gates its OWN harness — the shape this whole feature exists for.
        with open(os.path.join(home, "verify.yaml"), "w") as f:
            f.write('".game_loop/bin/*":\n  - "echo CHECK-RAN"\n')
        spgit("init", "-q")
        spgit("add", "-A")
        spgit("commit", "-q", "-m", "init")

        # The pinned CODE: bin/ only. The project's own files are deliberately absent — a second copy
        # of config.json and verify.yaml beside the code is a second identity, and the trap needs
        # only one misread of it to report a green nobody earned.
        pincode = os.path.join(pin, ".game_loop")
        shutil.copytree(home, pincode)
        for owned in ("config.json", "verify.yaml", "INVARIANTS.md", "LEDGER.md"):
            if os.path.exists(p := os.path.join(pincode, owned)):
                os.remove(p)

        # The change under test: the repo's own harness, edited, with no check run since.
        with open(os.path.join(home, "bin", "game_loop"), "a") as f:
            f.write("\n# an edit to the live harness\n")

        def run(binary, *args, home_val=None, code=None, stdin=None):
            env = _env(sp, sid="sess-pin")
            if home_val is not None:
                env["GAME_LOOP_HOME"] = home_val
            exe = os.path.join(code or pincode, "bin", binary)
            if not os.path.isfile(exe):
                # A binary that is not there is a FAILED expectation, not a crashed suite: these
                # tests are meant to be run against code that does not have the feature yet, and a
                # traceback there hides every result after it.
                return subprocess.CompletedProcess([exe], 127, "", f"no such binary: {exe}")
            return subprocess.run([exe, *args], input=stdin, capture_output=True, text=True,
                                  cwd=sp, env=env, timeout=60)

        def hook(script, payload, home_val=None, code=None):
            env = _env(sp, sid="sess-pin")
            if home_val is not None:
                env["GAME_LOOP_HOME"] = home_val
            return subprocess.run([os.path.join(code or pincode, "bin", script)],
                                  input=json.dumps(payload), capture_output=True, text=True,
                                  env=env, timeout=60)

        # CONTROL. What the repo's OWN gate says about that edit — the answer a pin must reproduce.
        own = run("verify", "--check", code=home)
        check("the repo's own verify refuses the harness edit (the answer a pin must preserve)",
              own.returncode == 1 and "VERIFY REFUSED" in own.stdout
              and ".game_loop/bin/game_loop" in own.stdout)

        # THE TRAP, built exactly: pinned code, nothing naming the home. It resolves the tree from
        # __file__, checks the PINNED directory — where nothing changed and no manifest exists — and
        # reports green about a repo it never looked at. This one PASSES IN BOTH STATES on purpose:
        # it asserts the HOLE, not the fix, so that the refusal on the next line is provably
        # load-bearing rather than decorative. Nothing else here would notice if it stopped being a
        # hole — and then the marker would be guarding nothing.
        naive = run("verify", "--check")
        check("a pinned copy told nothing about its home IS the trap — it reports green "
              "(passes in both states)",
              naive.returncode == 0 and "nothing owes a check" in naive.stdout)

        with open(os.path.join(pincode, "PINNED"), "w") as f:
            f.write("{}\n")
        blind = run("verify", "--check")
        check("...so a PINNED checkout run with no GAME_LOOP_HOME refuses instead of answering",
              blind.returncode == 2 and "PINNED code checkout" in blind.stderr
              and "nothing owes a check" not in blind.stdout)

        # THE FIX: the same pinned binary, told which home it serves, answers about the REPO.
        pinned = run("verify", "--check", home_val=home)
        check("pinned code with GAME_LOOP_HOME set checks the REPO, not the copy it runs from",
              pinned.returncode == 1 and "VERIFY REFUSED" in pinned.stdout
              and ".game_loop/bin/game_loop" in pinned.stdout)
        check("...and the manifest it read is the REPO's — the rule that fired exists only there",
              ".game_loop/bin/*" in pinned.stdout and "echo CHECK-RAN" in pinned.stdout
              and not os.path.exists(os.path.join(pincode, "verify.yaml")))

        # A home that cannot be trusted is refused, never guessed: a silent fallback to __file__ is
        # the trap again, wired by a typo instead of by omission.
        gone = os.path.join(pin, "no-such-home")
        r = run("verify", "--check", home_val=gone)
        check("a home that does not exist is refused, naming the file it looked for",
              r.returncode == 2 and "does not name a game_loop home" in r.stderr
              and os.path.join(gone, "config.json") in r.stderr)
        notgl = os.path.join(pin, "not-a-game-loop")
        os.makedirs(notgl)
        r = run("verify", "--check", home_val=notgl)
        check("a directory that exists but carries no config.json is refused the same way",
              r.returncode == 2 and "does not name a game_loop home" in r.stderr
              and os.path.join(notgl, "config.json") in r.stderr)
        r = run("verify", "--check", home_val="")
        check("an EMPTY value is refused too — 'read it as unset' is the silent fallback again",
              r.returncode == 2 and "(empty value)" in r.stderr)

        # Each entrypoint resolves its own home (a shared import is one more thing to break while the
        # harness is mid-edit), so the agreement BETWEEN them is what a test has to hold.
        entry = {"game_loop": run("game_loop", "status", home_val=gone),
                 "verify": run("verify", "--check", home_val=gone),
                 "watchdog": run("watchdog", home_val=gone, stdin="{}")}
        check("every python entrypoint refuses a bad home, not only the one that gates commits",
              len(entry) == 3
              and all(r.returncode == 2 and "does not name a game_loop home" in r.stderr
                      for r in entry.values()))
        w = hook("guard-writes.sh", {"tool_name": "Write",
                                     "tool_input": {"file_path": os.path.join(sp, "f.txt")}},
                 home_val=gone)
        check("the write guard refuses a bad home rather than enforcing some other project's policy",
              denied(w) and "does not name a game_loop home" in w.stdout
              and "allow_write_roots" in w.stdout)
        m = hook("guard-mcp.sh", {"tool_name": "mcp__x__write", "tool_input": {}}, home_val=gone)
        check("...and so does the MCP guard, whose config decides which calls merely READ",
              denied(m) and "does not name a game_loop home" in m.stdout)
        w = hook("guard-writes.sh", {"tool_name": "Write",
                                     "tool_input": {"file_path": os.path.join(sp, "f.txt")}})
        check("a PINNED guard run with no home refuses too — that wiring is the trap by omission",
              denied(w) and "PINNED code checkout" in w.stdout)

        # State in the pinned directory would be DESTROYED by the next upgrade, because upgrading IS
        # a re-checkout — and accumulated identity is exactly the thing that most needs to survive it.
        r = run("game_loop", "mandate", "--set", "pinned work", home_val=home)
        check("state written by pinned code lands in the HOME...",
              r.returncode == 0
              and os.path.isfile(os.path.join(home, "sessions", "sess-pin", "state.json")))
        check("...and so does the log — the pinned copy stays empty, since a re-pin deletes it",
              os.path.isfile(os.path.join(home, "log.jsonl"))
              and not os.path.exists(os.path.join(pincode, "log.jsonl"))
              and not os.path.exists(os.path.join(pincode, "sessions"))
              and not os.path.exists(os.path.join(pincode, "state.json")))

        st = run("game_loop", "status", home_val=home)
        check("status names BOTH locations, so the split is visible rather than inferred",
              "PINNED CODE" in st.stdout and pincode in st.stdout and home in st.stdout)
        check("...and says which of the two the rules and state actually come from",
              "read from and written to the HOME" in st.stdout)

        # End to end: the commit gate, fired by the real guard, running pinned code, still refusing
        # the REPO's unverified change. This is the whole feature in one assertion.
        spgit("add", "-A")
        cm = hook("guard-writes.sh", {"tool_name": "Bash", "session_id": "sess-pin", "cwd": sp,
                                      "tool_input": {"command": "git commit -m x"}}, home_val=home)
        check("the commit gate, run from pinned code, still refuses the repo's unverified change",
              denied(cm) and "VERIFY REFUSED" in cm.stdout
              and ".game_loop/bin/game_loop" in cm.stdout)
        # #36: the sentence doing the actual persuading appeared TWICE, back to back — once from
        # `verify --check` and once appended by the gate. It reads as a formatting bug at exactly
        # the moment the guard is asking to be taken seriously, which is the same currency #34 was
        # about. Counted, not eyeballed: "appears" would pass just as happily at two.
        _why = "evidence about code that no longer exists"
        check("...and the sentence that does the persuading appears exactly ONCE, not twice",
              cm.stdout.count(_why) == 1)
        check("...while the commit-specific escape it appends is still there",
              cm.stdout.count("--no-verify") == 1 and "./.game_loop/bin/verify" in cm.stdout)

        # THE COMMON PATH — variable unset, code and home the same directory. Every existing install
        # depends on this being untouched. These three PASS IN BOTH STATES by construction, and that
        # is precisely the claim being made about them.
        check("unset: the repo's own verify behaves exactly as before (passes in both states)",
              own.returncode == 1 and "VERIFY REFUSED" in own.stdout)
        plain = run("game_loop", "status", code=home)
        check("unset: status says nothing about pinning (passes in both states)",
              plain.returncode == 0 and "PINNED CODE" not in plain.stdout)
        # The probe under the HOME this guard resolves to (code == home, GAME_LOOP_HOME unset), for
        # the session hook() names: an allow here is silence, so the mark is what says it ran (#41).
        pin_probe = os.path.join(home, "sessions", "sess-pin", "write-guard-probe")
        pin_before = _probe_count(pin_probe)
        pw = hook("guard-writes.sh", {"tool_name": "Write",
                                      "tool_input": {"file_path": os.path.join(sp, "f.txt")}},
                  code=home)
        check("unset: the write guard still allows an in-repo write (passes in both states)",
              not denied(pw) and _probe_count(pin_probe) > pin_before)

        # `game_loop self` — a verb rather than a paragraph, because the checkout is two git commands
        # but the WIRING is what a human writes by hand, and the one wiring that recreates the trap
        # (pinned bin/, GAME_LOOP_HOME forgotten) is a step you can forget.
        r = run("game_loop", "self", "--pin", "HEAD", code=home)
        selfcode = os.path.join(sp, ".game_loop_self", ".game_loop")
        check("`self --pin` checks the harness out INSIDE the repo and nowhere else",
              r.returncode == 0 and os.path.isfile(os.path.join(selfcode, "bin", "game_loop"))
              and selfcode.startswith(sp + os.sep))
        check("...stamps VERSION and PINNED, so running it blind refuses instead of guessing",
              os.path.isfile(os.path.join(selfcode, "VERSION"))
              and os.path.isfile(os.path.join(selfcode, "PINNED")))
        # The checkout's existence is asserted first: "none of these four files is present" is
        # satisfied for free by a directory that was never created, and a check an empty answer
        # satisfies is a check that cannot fail.
        check("...drops the project's own files from the copy — one identity, never two",
              os.path.isfile(os.path.join(selfcode, "bin", "verify"))
              and not any(os.path.exists(os.path.join(selfcode, o))
                          for o in ("config.json", "verify.yaml", "INVARIANTS.md", "LEDGER.md")))
        check("...keeps the binaries executable, or the hooks would silently not run at all",
              os.access(os.path.join(selfcode, "bin", "guard-writes.sh"), os.X_OK))
        check("...and prints wiring that SETS the home — without it a pinned run checks the COPY",
              'GAME_LOOP_HOME="$CLAUDE_PROJECT_DIR/.game_loop"' in r.stdout)
        # The wiring goes in the TRACKED settings.json now, as ONE dispatching entry. The earlier
        # advice — a second copy in settings.local.json — was withdrawn because the two files MERGE
        # rather than override: every gate ran twice, and a commit printed the same blast-radius
        # warning verbatim two times. It fails quietly, duplicate output reading as the tool
        # repeating itself, so the instructions still have to say it.
        check("...as ONE dispatching entry in the tracked settings, not a second copy elsewhere",
              "TRACKED .claude/settings.json" in r.stdout
              and ".game_loop_self/.game_loop" in r.stdout and "|| d=" in r.stdout)
        check("...and still warns that the two settings files MERGE, which ran every gate twice",
              "MERGE" in r.stdout and "twice" in r.stdout
              and "DO NOT also wire" in r.stdout)
        # A dispatcher is only safe to TRACK because it degrades: no pin, no behaviour change. If
        # that stopped being true, shipping it would guard a fresh clone with a directory that is
        # not there.
        check("...and says the fallback is what makes tracking it safe",
              "falls back to the repo" in r.stdout)
        fresh = run("verify", "--check", code=selfcode)
        check("...and what it produced refuses to run blind, end to end",
              fresh.returncode == 2 and "PINNED code checkout" in fresh.stderr)
        bad = run("game_loop", "self", "--pin", "no-such-ref-anywhere", code=home)
        check("an unresolvable ref fails loudly and leaves the existing pin untouched",
              bad.returncode != 0 and "cannot resolve" in bad.stderr
              and os.path.isfile(os.path.join(selfcode, "bin", "game_loop")))
    finally:
        shutil.rmtree(sp, ignore_errors=True)
        shutil.rmtree(pin, ignore_errors=True)

    # CENTRAL INSTALL (`self --pin --dest`; `install.sh --central`). Every repo installing game_loop
    # today carries its OWN full copy of the tool. This lets many repos share ONE code location,
    # updated in one place, while each repo keeps only its own config/state — reusing the exact
    # CODE_ROOT/GAME_LOOP_HOME split the pinned-harness tests above already prove, just with the code
    # living at an arbitrary shared path instead of <repo>/.game_loop_self.
    print("central install (self --pin --dest; install.sh --central):")
    cc_glc = tempfile.mkdtemp(prefix="gameloop-central-glc-")
    shutil.rmtree(cc_glc)  # self --pin --dest must create it fresh, not merely use an empty dir
    cc_target = tempfile.mkdtemp(prefix="gameloop-central-target-")
    cc_plain = tempfile.mkdtemp(prefix="gameloop-central-plain-")
    try:
        gl_bin = os.path.join(SRC_GAME_LOOP, "bin", "game_loop")

        # Populate the shared central copy from THIS repo's own HEAD — the real, already-tested
        # self --pin machinery, just pointed at an arbitrary path instead of the repo-relative
        # default. Read-only against the repo (git archive of a committed ref); nothing here can
        # leave the real game_loop checkout dirty.
        pin_r = subprocess.run([gl_bin, "self", "--pin", "HEAD", "--dest", cc_glc],
                               cwd=REPO, capture_output=True, text=True, env=_env())
        check("self --pin --dest lands the checkout at the given path, not under the repo",
              pin_r.returncode == 0
              and os.path.isfile(os.path.join(cc_glc, ".game_loop", "bin", "game_loop"))
              and not os.path.join(cc_glc, ".game_loop").startswith(REPO + os.sep))
        check("...still strips the project's own identity files",
              not any(os.path.exists(os.path.join(cc_glc, ".game_loop", o))
                      for o in ("config.json", "verify.yaml", "INVARIANTS.md", "LEDGER.md")))
        # A BARE open() HERE TRUNCATED THE MUTATION SWEEP'S DENOMINATOR FOR ITS ENTIRE HISTORY.
        # The sweep runs against `git archive HEAD | tar -x`, which carries no .git, so `self --pin
        # HEAD` cannot resolve a ref, PINNED is never written, and this line raised — killing the
        # suite ~180 assertions early on EVERY sweep run. Those assertions could never flip, so any
        # producer they alone covered reported UNPROTECTED, which is the sweep's one failing verdict.
        # The check that fails is the one above; this line was never a check at all, only a way to
        # stop being one.
        try:
            with open(os.path.join(cc_glc, ".game_loop", "PINNED")) as f:
                pinned_marker = json.load(f)
        except (OSError, ValueError):
            pinned_marker = None
        check("...still stamps VERSION and PINNED, with home left null — many consumers, not one",
              pinned_marker is not None
              and os.path.isfile(os.path.join(cc_glc, ".game_loop", "VERSION"))
              and pinned_marker.get("home") is None)
        check("...still chmods the binaries executable, or the hooks would silently never run",
              os.access(os.path.join(cc_glc, ".game_loop", "bin", "guard-writes.sh"), os.X_OK))
        check("...points at install.sh --central instead of printing the in-repo hooks block",
              "install.sh --central" in pin_r.stdout
              and "TRACKED .claude/settings.json" not in pin_r.stdout)

        bad = subprocess.run([gl_bin, "self", "--dest", cc_glc], cwd=REPO, capture_output=True,
                             text=True, env=_env())
        check("--dest without --pin dies rather than doing nothing quietly",
              bad.returncode != 0 and "only meaningful with --pin" in bad.stderr)

        # install.sh --central against this real, populated central copy.
        subprocess.run(["git", "init", "-q"], cwd=cc_target)
        inst = subprocess.run([os.path.join(REPO, "install.sh"), "--central", cc_target],
                              capture_output=True, text=True, env=_env(GAME_LOOP_CENTRAL=cc_glc))
        check("install.sh --central succeeds and says it wrote dispatcher shims",
              inst.returncode == 0 and "dispatcher shims" in inst.stdout)
        bin_dir = os.path.join(cc_target, ".game_loop", "bin")
        check("...writes only the 5 shims — nothing needing a local copy under central dispatch",
              sorted(os.listdir(bin_dir)) == sorted(["game_loop", "guard-mcp.sh", "guard-writes.sh",
                                                      "verify", "watchdog"]))
        check("...and every one of them is a real dispatcher, not a copy of the full payload",
              all("GAME_LOOP_CENTRAL" in open(os.path.join(bin_dir, f)).read()
                  for f in os.listdir(bin_dir)))
        check("...owned files still seed exactly like a normal install",
              all(os.path.isfile(os.path.join(cc_target, ".game_loop", f))
                  for f in ("config.json", "verify.yaml", "INVARIANTS.md", "LEDGER.md")))

        # settings.json must be BYTE-IDENTICAL to a plain install — that is the entire point of
        # dispatching through local shims instead of rewriting hook command strings.
        subprocess.run(["git", "init", "-q"], cwd=cc_plain)
        subprocess.run([os.path.join(REPO, "install.sh"), cc_plain], capture_output=True, text=True,
                       env=_env())
        with open(os.path.join(cc_target, ".claude", "settings.json")) as f:
            central_settings = f.read()
        with open(os.path.join(cc_plain, ".claude", "settings.json")) as f:
            plain_settings = f.read()
        check("--central's settings.json is byte-identical to a plain install's — no command "
              "string changed, only what sits at the paths they already named",
              central_settings == plain_settings)

        # The real shims, doing real dispatching, through a REAL populated central copy.
        live_env = _env(cc_target, GAME_LOOP_CENTRAL=cc_glc)
        wr = subprocess.run([os.path.join(bin_dir, "guard-writes.sh")],
                            input=json.dumps({"tool_name": "Write",
                                              "tool_input": {"file_path": "/etc/passwd"}}),
                            capture_output=True, text=True, env=live_env)
        check("through the shim, the central write guard denies an out-of-repo write exactly like "
              "a normal install would",
              denied(wr) and "write outside this repo" in wr.stdout)
        st = subprocess.run([os.path.join(bin_dir, "game_loop"), "status"], cwd=cc_target,
                            capture_output=True, text=True, env=live_env)
        check("`status`, dispatched through the shim, prints the EXISTING pinned-code report "
              "unmodified — proving no new status code was needed for the happy path",
              "PINNED CODE" in st.stdout and cc_glc in st.stdout and cc_target in st.stdout)

        # Central MISSING: each shim degrades exactly per its own hook's existing, already-justified
        # posture — never a hard crash, and never silent either way.
        missing_env = _env(cc_target, GAME_LOOP_CENTRAL=os.path.join(cc_glc, "does-not-exist"))
        wr_missing = subprocess.run([os.path.join(bin_dir, "guard-writes.sh")],
                                    input=json.dumps({"tool_name": "Write",
                                                      "tool_input": {"file_path": "/etc/passwd"}}),
                                    capture_output=True, text=True, env=missing_env)
        check("write-guard shim fails OPEN when central is unreachable, and says so out loud",
              not denied(wr_missing) and "THE WRITE GUARD IS NOT RUNNING" in wr_missing.stdout)
        mc_missing = subprocess.run([os.path.join(bin_dir, "guard-mcp.sh")],
                                    input=json.dumps({"tool_name": "mcp__x__write", "tool_input": {}}),
                                    capture_output=True, text=True, env=missing_env)
        check("...while the MCP-guard shim fails CLOSED, for the opposite already-justified reason",
              denied(mc_missing) and "guard cannot run" in mc_missing.stdout)
        gl_missing = subprocess.run([os.path.join(bin_dir, "game_loop"), "limitgate"],
                                    capture_output=True, text=True, env=missing_env)
        check("...and the game_loop shim's HOOK subcommands fail open too — never block a live "
              "tool call over a missing central install",
              gl_missing.returncode == 0)
        gl_status_missing = subprocess.run([os.path.join(bin_dir, "game_loop"), "status"],
                                           capture_output=True, text=True, env=missing_env)
        check("...but its INTERACTIVE subcommands fail LOUD instead — nobody there is waiting on "
              "an exit code, they are waiting on an answer",
              gl_status_missing.returncode == 1 and "no central install at" in gl_status_missing.stderr)
        vr_missing = subprocess.run([os.path.join(bin_dir, "verify"), "--check"],
                                    capture_output=True, text=True, env=missing_env)
        check("...and the verify shim exits non-zero WITH output, so confidence --mark reads "
              "'cannot verify' rather than silently treating an absent binary as a clean tree",
              vr_missing.returncode != 0 and vr_missing.stdout.strip() != "")

        # Reverting: a bare re-install (no --central) must restore full local copies, loudly.
        revert = subprocess.run([os.path.join(REPO, "install.sh"), cc_target], capture_output=True,
                                text=True, env=_env())
        check("a bare re-install without --central restores full local copies",
              os.path.isfile(os.path.join(bin_dir, "guard-writes-impl.sh"))
              and os.path.isfile(os.path.join(bin_dir, "notify.py")))
        check("...and says so out loud rather than reverting silently",
              "reverted from central dispatch" in revert.stdout)
    finally:
        for d in (cc_glc, cc_target, cc_plain):
            shutil.rmtree(d, ignore_errors=True)

    # The documented `curl | bash` one-liner could install into a FRESH directory and could never
    # UPGRADE one. Piped, BASH_SOURCE[0] is empty; the old fallback to $0 made `dirname bash` = "."
    # so SRC became the cwd. On a fresh target the file test below it failed and the fetch happened
    # anyway — which is why installing into clean directories never showed it. On an upgrade the
    # target already had .game_loop/bin/game_loop, the test passed against the target's OWN old
    # copy, the fetch was skipped, SRC == TARGET, and it died claiming you had pointed it at the
    # game_loop repo. Reported from outside with a two-arm repro; the arms are pinned here.
    print("install.sh can reach a marked release (#61):")
    _inst = open(os.path.join(REPO, "install.sh")).read()
    # STRUCTURAL, because the behavioural arms need github. Each of these is a fact about the file
    # that was FALSE when the issue was filed, and each one was reported as measured against the
    # real endpoint: refs/heads/<tag> is 404, refs/tags/<tag> is 200.
    check("a tag is reachable — the fetch falls back to refs/tags when refs/heads misses, so the "
          "GAME_LOOP_REF=<tag> the header has always documented finally works",
          "refs/heads/$REF" in _inst and "refs/tags/$REF" in _inst)
    # CODE, NOT COMMENTS. The file explains the sort trap at length precisely so nobody reinvents
    # it, and a check that forbade the string outright would force that explanation to be deleted —
    # a rule that fires on the documentation of a hazard gets satisfied by removing the warning.
    _inst_code = "\n".join(l for l in _inst.split("\n") if not l.lstrip().startswith("#"))
    check("...and GAME_LOOP_CHANNEL resolves to a ref rather than sorting tags — the installer does "
          "no ordering, because the party that marked the commit already did it",
          'REF="$GAME_LOOP_CHANNEL"' in _inst_code and "--sort=" not in _inst_code)

    # THE DOCUMENTED ONE-LINER MUST PUT THE VARIABLE ON `bash`, NOT ON `curl`. In
    # `VAR=x curl … | bash`, the assignment applies to curl only — the bash that runs the installer
    # never sees it, so you silently get an alpha install of main. Reported against the recipe this
    # very issue added, and verified here rather than reasoned about: the broken form even stamps a
    # correct-looking VERSION, because main and the channel point at the same commit until main
    # moves. NOTHING IN THE TOOL CAN CATCH THIS — the installer never receives the variable, so that
    # run is indistinguishable from an ordinary one. The document is the only place it can be fixed,
    # which is exactly why the document is what gets checked.
    _readme = open(os.path.join(REPO, "README.md")).read()
    _broken = re.findall(r"^GAME_LOOP_\w+=\S+ curl[^\n]*\|\s*bash", _readme, re.M)
    check("no documented one-liner puts a GAME_LOOP_* assignment before `curl` in a pipe to bash — "
          "that form never reaches the installer and fails toward a silent alpha install",
          not _broken)
    check("...and the channel recipe is present in a form that DOES reach it, so this is a check on "
          "a live instruction rather than on the absence of one",
          re.search(r"^export GAME_LOOP_CHANNEL=stable$", _readme, re.M) is not None)
    check("...and setting CHANNEL and REF together is refused, since two sources for one answer "
          "leaves nobody able to say afterwards which was installed",
          "not both" in _inst and "GAME_LOOP_CHANNEL:-" in _inst)
    check("...and a level ref that was FETCHED records its level, instead of falling through to "
          "the alpha DEFAULT — a stable commit recording alpha is silent by construction",
          "LEVEL_REF" in _inst and 'GL_LEVEL="$LEVEL_REF"' in _inst)
    check("...covering BOTH grains: the moving channel pointer and one mark's immutable tag",
          "stable|stable-*)" in _inst and "beta|beta-*)" in _inst)
    # A CHANNEL NOBODY HAS MARKED YET IS AN ORDINARY STATE, not a transport failure. Before this,
    # GAME_LOOP_CHANNEL=beta with no beta pointer died as `curl: (56) 404` — which sends the reader
    # to debug their network instead of reading one sentence. Measured against the live repo, which
    # genuinely has a stable pointer and no beta one.
    check("...and a ref that does not exist names ITSELF and both paths tried, rather than exiting "
          "as a bare curl error",
          "both 404" in _inst and "refs/heads/$REF and refs/tags/$REF" in _inst)
    check("...and an unmarked CHANNEL says why it is missing and offers three real routes, since "
          "'nothing has been marked beta yet' is a normal answer and not a fault",
          "CHANNEL POINTER" in _inst and "GAME_LOOP_REF=<tag>" in _inst
          and "records alpha, honestly" in _inst)

    print("the sha lookup does not depend on a budget a team shares:")
    # MEASURED TWICE, the second time with the budget read at 0 of 60: the unauthenticated GitHub
    # REST API allows 60 requests an hour PER IP, shared by everything on the machine and by
    # everyone behind the same address. A team pulling this down on one afternoon exhausts it
    # between them, and every install after that gets no VERSION — and therefore a permanently
    # silent update check, which is indistinguishable from being up to date.
    _ish0 = open(os.path.join(REPO, "install.sh")).read()
    check("the installer resolves the sha over the GIT protocol before it touches the REST API, "
          "because that budget is shared by everyone behind one address",
          _ish0.index("git ls-remote") < _ish0.index("api.github.com/repos/$REPO/commits"))
    check("...and it dereferences an ANNOTATED tag before falling back to the plain ref, so a "
          "marked release resolves to its commit rather than to the tag object",
          '"$REF^{}"' in _ish0)
    check("...and the REST call survives as the last resort, so a machine with no git still "
          "stamps rather than silently losing its update check",
          "api.github.com/repos/$REPO/commits" in _ish0 and "command -v git" in _ish0)

    print("a missing VERSION stamp says what it COSTS, not only that it is missing:")
    # MEASURED: an install from the stable channel produced no stamp, and the warning explained the
    # mechanism without saying the consequence — that `status` can no longer tell you an update
    # exists, going QUIET rather than wrong, which is indistinguishable from up to date. The cause
    # was the unauthenticated GitHub API's 60/hour limit, shared by everything on the machine; the
    # same install stamped correctly minutes later. A consumer who hits that window would never
    # learn their update check was dead.
    _ish = open(os.path.join(REPO, "install.sh")).read()
    check("the no-stamp warning names the CONSEQUENCE — a reader needs to know their update check "
          "just went silent, not only that a file is absent",
          "WHAT IT COSTS YOU" in _ish and "indistinguishable from up to date" in _ish)
    check("...and it names the likeliest causes and the durable remedy, since an exhausted budget "
          "and a GitHub incident produce the SAME silence and one reading cannot separate them",
          "60 requests" in _ish and "incident" in _ish and "Installing git avoids both" in _ish)

    print("every executable in bin/ actually ships (default-deny over the payload):")
    # MEASURED, NOT IMAGINED: limit-probe.sh was added to bin/, wired into the verb and the
    # watchdog, tested, and NEVER ADDED TO install.sh. Every consumer would have got
    # "the probe script is missing" — a feature shipped broken, invisible from inside the repo
    # because the file is right there. The list in install.sh is a denylist by omission: it
    # defaults to NOT shipping whatever nobody remembered to type.
    _bin = os.path.join(SRC_GAME_LOOP, "bin")
    _shipped_from = open(os.path.join(REPO, "install.sh")).read()
    _exes = sorted(n for n in os.listdir(_bin)
                   if os.access(os.path.join(_bin, n), os.X_OK)
                   and not n.startswith(".") and n != "__pycache__")
    # Imported by the others rather than invoked, so they ride along in the same cp line; named here
    # so "not executable" never becomes the reason something silently stops shipping.
    _also = {"flair.py", "notify.py"}
    _missing = [n for n in _exes + sorted(_also) if n not in _shipped_from]
    check(f"every executable in .game_loop/bin/ is named in install.sh ({len(_exes)} found)"
          + (" · NOT SHIPPED: " + ", ".join(_missing) if _missing else ""),
          _exes and not _missing)
    check("...and the check can FIRE — a name that is not in the installer is reported, so the "
          "clean result above is a verdict rather than a loop over nothing",
          "definitely-not-in-install" not in _shipped_from
          and [n for n in ["definitely-not-in-install.sh"] if n not in _shipped_from])
    check("...and the sandbox fixture ships the same set, so a test install cannot pass on a file "
          "no real install would have",
          all(os.path.exists(os.path.join(make_sandbox(), ".game_loop", "bin", n))
              for n in ("limit-probe.sh", "game_loop")))

    print("install.sh refuses to DOWNGRADE a blessed install (I did this to a consumer):")
    # I committed the vendored refusal above, whose message says "a warning printed after the copy is
    # a report, not a guard" -- and then installed my working tree over a consumer an hour later,
    # turning its CONFIDENCE from stable to alpha. The installer DID warn. It warns ~160 lines after
    # the copy, beside "Done", so it described something already done, and I skimmed it while
    # grepping the output for "Done". The vendored check does not cover this: that repo carries no
    # packager marker. The condition that matters is the DOWNGRADE, and it is knowable before copying.
    dg = tempfile.mkdtemp(prefix="gameloop-downgrade-")
    try:
        src = os.path.join(dg, "src")
        shutil.copytree(REPO, src, ignore=shutil.ignore_patterns(
            ".git", ".worktrees", ".game_loop_self", "__pycache__"))
        # A HEAD that exists and carries no beta-*/stable-* tag, built rather than assumed: this
        # repo's own HEAD may or may not be tagged on the machine running the suite.
        for a in (["init", "-q", "."], ["add", "-A"],
                  ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "untagged"]):
            subprocess.run(["git", *a], cwd=src, capture_output=True)
        tgt = os.path.join(dg, "tgt")
        os.makedirs(os.path.join(tgt, ".game_loop"))
        subprocess.run(["git", "init", "-q", "."], cwd=tgt, capture_output=True)
        _inst2 = os.path.join(src, "install.sh")

        def _at(level, *flags):
            with open(os.path.join(tgt, ".game_loop", "CONFIDENCE"), "w") as f:
                f.write(level + "\n")
            return subprocess.run(["bash", _inst2, *flags, tgt],
                                  capture_output=True, text=True, env=_env())

        _down = _at("stable")
        check("an unblessed source over a STABLE install is REFUSED before the copy — the existing "
              "notice prints beside 'Done', which describes a downgrade already made",
              _down.returncode != 0 and "DOWNGRADE" in _down.stderr)
        check("...and the refusal names marking the commit as the remedy, not the escape, because a "
              "refusal whose only exit is a force flag teaches the force flag",
              "confidence --mark stable" in _down.stderr)
        check("...while the same install over an ALPHA target proceeds — a fresh install from an "
              "unmarked commit is ordinary, and a check that fired there would break every install",
              _at("alpha").returncode == 0)
        check("...and the named escape proceeds, since a BROKEN blessed install is exactly the case "
              "that needs the installer (INV5)",
              _at("stable", "--over-blessed").returncode == 0)
        # THE REGRESSION THIS GUARD SHIPPED WITH, reported by one consumer and reproduced by a
        # second within the hour: it blocked EVERY packager-vendored install. A packager vendors the
        # payload WITHOUT .git, so there are no tags, the level fell through to ALPHA, and the guard
        # refused correctly on an input that was never about the payload in front of it.
        #
        # install.sh already solved this BELOW the copy — its level logic prefers the source
        # payload's own CONFIDENCE when the source is not its own checkout. The pre-copy guard asked
        # git and stopped. Same question, two code paths, consulted in one: the defect a consumer
        # reported to me that morning about installed-by.json, reintroduced by the fix for it.
        vend_src = os.path.join(dg, "vendored")
        shutil.copytree(src, vend_src, ignore=shutil.ignore_patterns(".git"))   # as a packager ships
        _vinst = os.path.join(vend_src, "install.sh")

        def _vend(level, tgt_level):
            with open(os.path.join(vend_src, ".game_loop", "CONFIDENCE"), "w") as f:
                f.write(level + "\n")
            with open(os.path.join(tgt, ".game_loop", "CONFIDENCE"), "w") as f:
                f.write(tgt_level + "\n")
            return subprocess.run(["bash", _vinst, tgt], capture_output=True, text=True, env=_env())

        check("a VENDORED source (no .git) stamped stable installs over a stable target — the "
              "payload carries the answer in game_loop's own format, and asking git about a tree "
              "with no tags is a question about the wrong thing",
              _vend("stable", "stable").returncode == 0)
        check("...and the same vendored source stamped ALPHA is still refused, so reading the "
              "payload's stamp did not disarm the guard, it corrected its input",
              _vend("alpha", "stable").returncode != 0)
        # ORDERING, not mere presence: a source checkout that has ACQUIRED a stamp (installing
        # game_loop into itself writes one) must still be judged by its live tags, or a stale file
        # would outrank the repo it sits in.
        with open(os.path.join(src, ".game_loop", "CONFIDENCE"), "w") as f:
            f.write("alpha\n")
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "tag", "-a",
                        "stable-deadbeef", "-m", "blessed"], cwd=src, capture_output=True)
        check("...while a source that IS its own checkout is judged by its TAGS even when a stale "
              "CONFIDENCE file sits beside them — the payload stamp is the fallback, not the winner",
              _at("stable").returncode == 0)
    finally:
        shutil.rmtree(dg, ignore_errors=True)

    print("install.sh refuses to overwrite a VENDORED payload (found by a consumer, about my advice):")
    # I surveyed every repo's .game_loop/VERSION and told three agents to run `./install.sh <repo>`
    # from my checkout. For a VENDORED consumer that installs whatever is in the source working tree
    # at that instant, replaces a blessed stamped release with an unblessed one, and undoes the
    # vendoring -- the exact remedy installed-by.json exists to redirect. update_notice() and
    # `status` both already read the marker and name the packager's command; install.sh, the one path
    # that DOES the damage, never looked. Same question, three code paths, consulted in two.
    #
    # I then ran it over two repos carrying the marker. Harmless only by timing: my tree happened to
    # be exactly stable, so they landed on the sha the packager would have given them.
    vend = tempfile.mkdtemp(prefix="gameloop-vendored-")
    try:
        _inst = os.path.join(REPO, "install.sh")
        subprocess.run(["git", "init", "-q", "."], cwd=vend, capture_output=True)
        _ok = subprocess.run(["bash", _inst, vend], capture_output=True, text=True, env=_env())
        check("a target with NO vendor marker installs — the paired arm, or a refusal that always "
              "fires would read as protection while breaking every install",
              _ok.returncode == 0
              and os.path.isfile(os.path.join(vend, ".game_loop", "bin", "game_loop")))
        with open(os.path.join(vend, ".game_loop", "installed-by.json"), "w") as f:
            json.dump({"name": "somepkg", "upgrade": "somepkg upgrade game_loop"}, f)
        _no = subprocess.run(["bash", _inst, vend], capture_output=True, text=True, env=_env())
        check("...and the SAME target, once marked as vendored, is REFUSED — the installer is what "
              "undoes the vendoring, so the check belongs where the copy happens",
              _no.returncode != 0 and "VENDORED" in _no.stderr)
        check("...and the refusal names the PACKAGER'S OWN command, so the reader is redirected "
              "rather than merely stopped — a refusal with no remedy is how people reach for --force",
              "somepkg upgrade game_loop" in _no.stderr)
        _esc = subprocess.run(["bash", _inst, "--over-vendored", vend],
                              capture_output=True, text=True, env=_env())
        check("...and the named escape proceeds, because INV5 says a guard must never block its own "
              "fix: a vendored install that is BROKEN still has to be repairable by the installer",
              _esc.returncode == 0)
        # ...AND THE DEADLOCK THAT GUARD SHIPPED WITH. A packager upgrade runs the payload vendored
        # INSIDE the consumer, so the guard fired on the packager's OWN upgrade and named that same
        # command as the remedy. No forward move: the only other option offered undoes the vendoring
        # the guard exists to protect.
        #
        # It hit exactly the consumers the marker was built for -- the ones who declared themselves.
        # Consumers who ignored the feature were unaffected. A guard that punishes adoption teaches
        # people to delete the declaration, and the consumer who found this refused to, on the
        # grounds that removing it would make their repo lie about its own provenance.
        #
        # The discriminator needs no flag and no contract: nothing but a vendored payload can be
        # INSIDE the tree it is installing into.
        inner = os.path.join(vend, '.lamp/game_loop')
        shutil.copytree(REPO, inner, ignore=shutil.ignore_patterns(
            ".git", ".worktrees", ".game_loop_self", "__pycache__"))
        with open(os.path.join(inner, ".game_loop", "CONFIDENCE"), "w") as f:
            f.write("stable\n")
        with open(os.path.join(vend, ".game_loop", "installed-by.json"), "w") as f:
            json.dump({"name": "somepkg", "upgrade": "somepkg upgrade game_loop"}, f)
        _pkg = subprocess.run(["bash", os.path.join(inner, "install.sh"), vend],
                              capture_output=True, text=True, env=_env())
        check("a payload vendored INSIDE the consumer installs into that consumer — that is the "
              "packager's own upgrade path, and refusing it deadlocks the command the refusal names",
              _pkg.returncode == 0)
        # TWO GUARDS CAN FIRE HERE AND THE ASSERTION BELOW IS ABOUT ONE OF THEM. The packager
        # install just stamped this target CONFIDENCE=stable, so the DOWNGRADE guard refuses first
        # whenever the suite runs from a commit carrying no stable tag — the hand install still
        # exits 1, for a different reason than the one under test, and the check reads as a
        # regression in the vendored guard. Neutralise the other condition rather than assert on
        # whichever refusal happens to win.
        with open(os.path.join(vend, ".game_loop", "CONFIDENCE"), "w") as f:
            f.write("alpha\n")
        _hand = subprocess.run(["bash", _inst, vend], capture_output=True, text=True, env=_env())
        check("...while a hand install from a separate checkout is still REFUSED, so standing aside "
              "for the packager did not disarm the guard for the case it was written for",
              _hand.returncode != 0 and "VENDORED" in _hand.stderr)
        check("...and the refusal NAMES the file that triggered it — the consumer who found the "
              "deadlock had to bisect by moving the marker aside, because the text never said",
              "installed-by.json" in _hand.stderr)
        check("...and the refusal states every CONDITION it tested, not just the marker it found — "
              "two consumers asked for this independently, one after bisecting to find the trigger",
              "WHAT WAS TESTED" in _hand.stderr
              and "SOMEPKG_INSTALL" in _hand.stderr
              and "not inside this project" in _hand.stderr.replace("NOT", "not"))
        # A DECLARED CALLER, for packagers the path test cannot see: one vendoring into a shared
        # store rather than into the consumer. The variable is DERIVED from the marker's own name, so
        # this file still names no package manager -- the principle that produced the better fix
        # earlier today. A packager calling itself `somepkg` sets SOMEPKG_INSTALL.
        _declared = dict(_env(), SOMEPKG_INSTALL="1")
        _dec = subprocess.run(["bash", _inst, vend], capture_output=True, text=True, env=_declared)
        check("...while the packager DECLARING itself the caller proceeds, derived from the marker's "
              "own name so no packager is hardcoded here",
              _dec.returncode == 0)
        _wrong = subprocess.run(["bash", _inst, vend], capture_output=True, text=True,
                                env=dict(_env(), OTHERPKG_INSTALL="1"))
        check("...and a DIFFERENT packager's variable does not open the door — the signal is scoped "
              "to whoever the marker says placed the payload, not to any caller who sets something",
              _wrong.returncode != 0)
    finally:
        shutil.rmtree(vend, ignore_errors=True)

    print("an exclusion cannot become invisible by how its reason is WORDED:")
    # The sweep reports KNOWN GAPS by matching a PROSE PREFIX on each NOT_SWEPT reason. Two
    # producers written an hour earlier began "UNMEASURED", so they were neither swept nor listed —
    # excluded twice over, by phrasing, while the run printed "no producer is unprotected". The
    # denominator failure this file exists to prevent, inside the file that prevents it.
    #
    # NOT fixed by naming every unmatched entry: 32 of 39 are legitimate permanent exclusions whose
    # reasons simply start with other words, and a report with that noise ratio gets deleted. Fixed
    # by making the SHAPE enforced — an unrecognised prefix fails here rather than disappearing.
    # READ the table, never EXECUTE the module: importing mutation_sweep runs its top level, and
    # this block sat in a suite that ran 20+ minutes instead of 3 before I noticed. A test that
    # executes the thing it is asking a question about is paying that thing's full cost to read one
    # dict.
    _msrc = open(os.path.join(REPO, "test", "mutation_sweep.py")).read()
    _tree = ast.parse(_msrc)
    _ns = {}
    for _node in _tree.body:
        if isinstance(_node, ast.Assign) and any(
                getattr(t, "id", "") == "NOT_SWEPT" for t in _node.targets):
            _ns = ast.literal_eval(_node.value)
    # I FIRST DEMANDED A PREFIX VOCABULARY of every entry, and backed it out: 26 of 39 reasons open
    # with things like "a loader", "a normalizer", "helper of config_paths_report" — good prose that
    # explains, and forcing a keyword onto it is 26 rewrites plus a format to fight forever, to
    # prevent something small. The narrow defect was never the vocabulary; it was that a DEBT
    # written in another shape is invisible to a report that greps for one. So the debts are pinned
    # by name, and the prose stays free.
    # THE ASSERTION MOVED WITH THE FACT. It first pinned these two as declared KNOWN GAPS, because
    # they were unswept AND unlisted — invisible by how their reason happened to be worded, while
    # the run printed "no producer is unprotected". They are now SWEPT, with floors measured at 3
    # and 4 by a 28-minute run of all 47 producers, which is strictly better than being declared.
    # So this pins the stronger property, and the weaker one is gone because it stopped being true.
    _mut_src = open(os.path.join(REPO, "test", "mutation_sweep.py")).read()
    check("the two producers gating turn-end are SWEPT with measured floors, not excluded — an "
          "entry in NOT_SWEPT is invisible to the instrument by construction, so paying 'floor owed "
          "on the next sweep' meant ceasing to exclude them",
          all(f'game_loop::{n}"' in _mut_src for n in ("retro_overdue", "retro_debt_open"))
          and not [k for k in _ns
                   if k.endswith("::retro_overdue") or k.endswith("::retro_debt_open")])
    check("...and the KNOWN GAP category is not empty, or the check above would pass by there being "
          "nothing to classify",
          any((w or "").strip().startswith("KNOWN GAP") for w in _ns.values()))

    print("a tree's FIRST retro counts its chapter, rather than reporting nothing (#77):")
    # "No previous retro" returned "this is the first" and counted NOTHING, so everything encoded
    # before a tree's first stepback fell into a window no retro ever read — real hardens, invisible,
    # counter reading zero. Found while probing a consumer report whose stated MECHANISM did not
    # reproduce: their symptom was real, their cause was not this, and the probe written to check
    # them surfaced this instead. A non-reproduction that produces a different true finding.
    fw = make_sandbox()
    _art = os.path.join(fw, ".game_loop", "config.json")
    gl(fw, "harden", "--learning", "x", "--artifact", _art, "--mechanism", "y", "--rung", "3",
       sid="sess-fr")
    _first = gl(fw, "stepback", "--notes", "the first retro of this tree", sid="sess-fr").stdout
    check("the FIRST retro reports the work encoded before it — 'no previous retro' is not 'no "
          "previous work', and counting nothing made a real harden invisible to every later reader",
          "1 hardened" in _first)
    check("...and it says WHICH window it counted, so a reader cannot mistake a whole-log count for "
          "a since-last-retro one",
          "beginning of this tree's record" in _first)
    _second = gl(fw, "stepback", "--notes", "second", sid="sess-fr").stdout
    check("...while the SECOND retro goes back to counting since the last one — the paired arm, or "
          "every retro would report the whole log forever",
          "beginning of this tree's record" not in _second)

    print("checkpoint is the escape that always works, so it gets ritualised (#75):")
    # Measured from ONE session — mine: 16 checkpoints, 14 stop-gate blocks, 3 mandate events. The
    # gate refused fourteen turn-ends and was paid off sixteen times, and the human had to say twice
    # that there was no reason to stop. Identical shape to #74, which I had fixed hours earlier:
    # `arm` needs a real question, `--clear` needs the work done, `--park` needs a human's break.
    # `checkpoint` needs nothing, so it wins. NOT gated: pricing it pushes the next agent toward
    # `--park`, which lies about a human having called a break. The RECORD is what changes.
    rq = make_sandbox()
    _rlog = os.path.join(rq, ".game_loop", "log.jsonl")

    def _cp(msg):
        return gl(rq, "checkpoint", "--notes", msg, sid="sess-rit").stdout

    def _log(kind):
        with open(_rlog, "a") as f:
            f.write(json.dumps({"t": "2026-08-17T00:00:00", "kind": kind}) + "\n")

    check("one checkpoint against a clean log says nothing — reporting progress under a mandate is "
          "legitimate, and a warning that fires on the first one would be noise people learn to skip",
          "CHECKPOINTS SINCE" not in _cp("first"))
    _log("checkpoint"); _log("checkpoint")
    check("...but a RUN of them with no claim, harden, proof, phase or retro between is named — that "
          "is not progress reported N times, it is the same turn-end bought N times",
          "CHECKPOINTS SINCE" in _cp("third"))
    _log("harden")
    check("...and any real evidence work RESETS it, so the counter measures ritual rather than "
          "counting checkpoints, which a working session legitimately accumulates",
          "CHECKPOINTS SINCE" not in _cp("after work"))
    # THE HALF THE AGENT DOES NOT AUTHOR. Every name in the verb set is one the agent CHOOSES to
    # call, so a session doing real work without narrating it gets told it is ritualising — and the
    # remedy it reaches for is to call a verb rather than to do something. A consumer built this same
    # counter, made the same choice, and measured the consequence before I had.
    _rgit = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                                       "-c", "commit.gpgsign=false", *a], cwd=rq,
                                      capture_output=True, text=True)
    _rgit("init", "-q", ".")
    _log("checkpoint"); _log("checkpoint")
    check("...a run of checkpoints with NO commit and no verb is still named — the paired arm, or "
          "the commit check below would be indistinguishable from the counter never firing",
          "CHECKPOINTS SINCE" in _cp("stalled"))
    _rgit("commit", "-q", "--allow-empty", "-m", "real work, never narrated")
    check("...but a COMMIT since those checkpoints resets it, because work that landed in git is "
          "work whether or not the agent called a verb about it",
          "CHECKPOINTS SINCE" not in _cp("committed"))

    print("a gate can be a plausible local approximation of CI and report the same green (#72):")
    # A consumer ran `flutter analyze lib` for a year against a CI running `dart analyze
    # --fatal-infos` over the whole tree. Different scope, different severity, neither visible by
    # reading the rule. I first shipped only a WARNING IN PROSE — enforcement by instruction, for an
    # issue whose framing is "the check that cannot fail, one level up" — and declined the real
    # check because this repo has no CI to exercise it against. That reason does not survive: this
    # suite runs on invented sandboxes and invented configs, and a workflow file is no different.
    cw = make_sandbox()
    _wf = os.path.join(cw, ".github", "workflows")
    os.makedirs(_wf, exist_ok=True)
    _cov = lambda: subprocess.run([os.path.join(cw, ".game_loop", "bin", "verify"), "--coverage"],
                                  cwd=cw, capture_output=True, text=True, env=_env(cw)).stdout
    check("with NO workflows it SAYS there is nothing to compare against, rather than printing a "
          "clean cross-check that reads as cover it does not have",
          "nothing to compare against" in _cov())
    with open(os.path.join(cw, ".game_loop", "verify.yaml"), "a") as f:
        f.write('\n"src/**":\n  - "python3 test/run.py"\n')
    with open(os.path.join(_wf, "ci.yml"), "w") as f:
        f.write("jobs:\n  t:\n    steps:\n      - run: dart analyze --fatal-infos app/\n"
                "      - run: |\n          python3 test/run.py\n")
    _out = _cov()
    check("...and a CI command that NO gate runs is named, which is the difference a human could "
          "not see by reading the rule",
          "dart analyze --fatal-infos" in _out and "NO GATE RUNS" in _out)
    check("...while a CI command a gate DOES run is not reported — it discriminates rather than "
          "listing every line of the workflow, which is the noise ratio that gets a check deleted",
          "python3 test/run.py" not in _out.split("NO GATE RUNS")[1].split("Paste CI")[0])
    check("...and it says it compares TEXT and not behaviour, so two spellings of one check read as "
          "a difference and the report stays a difference for a human to judge, never a verdict",
          "COMPARES COMMAND TEXT, NOT BEHAVIOUR" in _out)

    print("a commit into an unharnessed tree is REFUSED, and the refusal is not invisible (#74):")
    # The refusal is right and its REMEDY SHAPE was the problem: install-it-there is a manual step
    # nobody is prompted to take, commit-from-the-parent is impossible when the changes are in the
    # worktree, and --no-verify always works. So the escape wins every time. Four of them in one day
    # across two hand-made worktrees, each honestly documented in the commit message.
    #
    # AND THE PATTERN WAS INVISIBLE: the worktree has no .game_loop/, so no log.jsonl, so the
    # PARENT's log recorded zero of these. Grepping the parent — where a reviewer looks — found
    # nothing. A guard that cannot report how often it is bypassed cannot be argued about.
    wt = make_sandbox()

    def _wgit(*a):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                               "-c", "commit.gpgsign=false", *a], cwd=wt,
                              capture_output=True, text=True)

    _wgit("init", "-q", ".")
    _wgit("commit", "-q", "--allow-empty", "-m", "base")
    _hand = os.path.join(wt, ".worktrees", "hand")
    _wgit("worktree", "add", "-q", _hand, "-b", "hand")
    _gl_dir = os.path.join(wt, ".game_loop")
    _log = os.path.join(_gl_dir, "log.jsonl")
    _before = len(open(_log).read().splitlines()) if os.path.exists(_log) else 0
    _r = subprocess.run(["bash", os.path.join(_gl_dir, "bin", "guard-writes.sh")],
                        input=json.dumps({"tool_name": "Bash", "tool_input": {
                            "command": f"git -C {_hand} commit -m x"}}),
                        capture_output=True, text=True,
                        env=dict(os.environ, CLAUDE_PROJECT_DIR=wt, GAME_LOOP_HOME=_gl_dir))
    check("committing into a worktree that carries no harness is still REFUSED — its owed checks "
          "cannot be read, and reading a different tree's record would report confidence either way",
          "deny" in (_r.stdout or ""))
    check("...and the refusal names a PASTEABLE install for that exact path, so the honest remedy "
          "costs one line rather than being a manual step nobody is prompted to take",
          "install.sh" in (_r.stdout or "") and _hand in (_r.stdout or ""))
    _after = len(open(_log).read().splitlines()) if os.path.exists(_log) else 0
    check("...and it is recorded in the PARENT's log, the only tree here that has one — otherwise "
          "every bypass it prompts is invisible where a reviewer would grep for the pattern",
          _after > _before
          and any(json.loads(l).get("kind") == "commit_gate_unharnessed_tree"
                  for l in open(_log).read().splitlines() if l.strip()))

    print("a revert-proof the TOOL performs, because a reported one reads the same either way (#71):")
    # "Reverted the fix, watched it fail, restored" is ONE SENTENCE whether or not it happened. A
    # consumer fanned out eight leaves that each reported exactly that; an independent reviewer
    # mutation-tested the branch and THREE OF EIGHT stayed green with the bug reintroduced.
    mt = tempfile.mkdtemp(prefix="gameloop-mutate-")
    try:
        _prod = os.path.join(mt, "prod.py")
        with open(_prod, "w") as f:
            f.write("def add(a, b):\n    return a + b\n")
        for nm, body in (("t_good.py", "from prod import add\nassert add(2,2)==4\n"),
                         ("t_weak.py", "from prod import add\nassert add is not None\n")):
            with open(os.path.join(mt, nm), "w") as f:
                f.write(body)

        def _mut(test, extra=()):
            return gl(REPO_SANDBOX_NA if False else mtw, "mutate", "--prove", "add adds",
                      "--test", f"cd {mt} && python3 {test}", "--file", _prod,
                      "--replace", "a + b", "--with", "a - b", *extra)

        mtw = make_sandbox()
        _good = _mut("t_good.py")
        check("a test that DOES pin the line proves it — the tool ran both halves itself, so no "
              "sentence could have produced this receipt",
              _good.returncode == 0 and "PROVED" in _good.stdout)
        _weak = _mut("t_weak.py")
        check("...and a test that passes with the bug reintroduced is REFUSED, which is the finding "
              "worth having: three of eight leaves were in exactly this state",
              _weak.returncode != 0 and "NOT PROVED" in (_weak.stdout + _weak.stderr))
        check("...and the tree is RESTORED either way — a verb that mutates somebody's source and "
              "leaves it mutated is worse than the self-report it replaces",
              open(_prod).read().strip().endswith("return a + b"))
        # A SAME-SIZE MUTATION IS INVISIBLE to any cache keyed on (mtime, size). This verb reported
        # NOT PROVED for a test that demonstrably goes red, because `a + b` -> `a - b` is the same
        # length and both writes landed in one mtime tick, so Python reused its .pyc. The verb built
        # to catch a test that cannot fail was itself returning a confident wrong verdict.
        check("...and a same-SIZE mutation still registers, so a stale bytecode cache cannot make a "
              "working test look like one that pins nothing",
              len("a + b") == len("a - b") and _good.returncode == 0)
    finally:
        shutil.rmtree(mt, ignore_errors=True)

    print("a retro that encodes nothing did not happen — and an ignored nudge becomes a gate:")
    # Reported by a human running several machines: one ran `stepback`, produced a full reflection
    # and hardened nothing; others ignore the nudge entirely and, asked later, will say the retro is
    # past due. The verb PRINTS the ladder and the exact harden command -- an instruction, which is
    # the one thing INV1 says never to enforce with. Its only real check fired at the NEXT retro,
    # days later, usually to a different session.
    rw = make_sandbox()

    def _gate(sid):
        return subprocess.run([os.path.join(rw, ".game_loop", "bin", "game_loop"), "stopgate"],
                              cwd=rw, input=json.dumps({"session_id": sid}),
                              capture_output=True, text=True, env=_env(rw, sid=sid))

    check("a session with no retro and no mandate ends its turn — the control, or every arm below "
          "would be a gate that simply always closes",
          _gate("sess-r1").returncode == 0)
    gl(rw, "stepback", "--notes", "a real retro naming real learnings", sid="sess-r1")
    _held = _gate("sess-r1")
    check("...and after a RETRO with nothing encoded, turn-end is HELD — the reflection was never "
          "the point, and a retro that encodes nothing is indistinguishable a week later from one "
          "that never happened",
          _held.returncode == 2 and "encoded nothing" in _held.stderr)
    check("...and the refusal names BOTH ways out, because a gate with no honest exit is one people "
          "route around rather than satisfy",
          "game_loop harden" in _held.stderr and "--nothing-to-harden" in _held.stderr)
    _art = os.path.join(rw, ".game_loop", "config.json")
    _h = gl(rw, "harden", "--learning", "x", "--artifact", _art, "--mechanism", "y", "--rung", "3",
            sid="sess-r1")
    check("...and a SUCCEEDING harden pays the debt, so doing the right thing opens the gate — the "
          "arm I first read as broken, from a harden that had been refused for a bad artifact path",
          _h.returncode == 0 and _gate("sess-r1").returncode == 0)
    gl(rw, "stepback", "--notes", "another chapter", sid="sess-r1")
    _dec = gl(rw, "stepback", "--nothing-to-harden", "--reason", "this chapter taught nothing",
              sid="sess-r1")
    check("...and DECLINING on the record opens it too: an empty chapter is a legitimate outcome, "
          "and it costs one sentence that lands in the log rather than a silence",
          _dec.returncode == 0 and _gate("sess-r1").returncode == 0)
    _refused = gl(rw, "stepback", "--nothing-to-harden", sid="sess-r1")
    check("...while declining with NO reason is refused — a decision with no reason recorded is "
          "indistinguishable from the silence this gate exists to stop",
          _refused.returncode != 0)

    # THE NUDGE ITSELF, escalating. A printed nudge is a thing to remember; agents pass over it and
    # never re-check. It stays advice for a whole threshold, then closes.
    _sd = os.path.join(rw, ".game_loop", "sessions", "sess-r2")
    os.makedirs(_sd, exist_ok=True)
    _sf = os.path.join(_sd, "state.json")

    def _work(n):
        try:
            with open(_sf) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        d["work_since_stepback"] = n
        with open(_sf, "w") as f:
            json.dump(d, f)

    _work(8)
    check("at the NUDGE threshold the turn still ends — an agent mid-chapter is not yanked out of "
          "it, which is why the gate sits a full threshold further out",
          _gate("sess-r2").returncode == 0)
    _work(16)
    _over = _gate("sess-r2")
    check("...and at twice the threshold the nudge becomes a GATE, because `status` printing 'due' "
          "is advice and this project's first invariant is that advice is followed only sometimes",
          _over.returncode == 2 and "overdue" in _over.stderr)

    print("the one tracked file whose contents EXECUTE in somebody else's checkout:")
    # .claude/settings.json is the only tracked thing here that RUNS in a cloner's machine, and it
    # is in verify.yaml's unchecked-ok list -- "prose, no command exists that fails when they are
    # broken" was true of the rest of that list and never of this file. A home-anchored path in it
    # was reported once before; the installer was fixed and nothing stopped it coming back.
    #
    # Passed on by llm_chat's owner, who added the same check after their own verify reported this
    # file matched no rule. THE THIRD ASSERTION IS THEIRS and is the one I would have missed: a scan
    # over an empty hook list passes while examining nothing.
    _set = os.path.join(REPO, ".claude", "settings.json")
    with open(_set) as f:
        _raw = f.read()
    _cfg = json.loads(_raw)
    _hooks = [h for group in (_cfg.get("hooks") or {}).values() for entry in group
              for h in (entry.get("hooks") or [])]
    check("the tracked settings.json carries NO home-anchored path — one there ships a machine's "
          "own layout to every cloner, and it executes rather than merely misleading",
          "/Users/" not in _raw and "$HOME" not in _raw and '"~/' not in _raw)
    _cmds = [str(h.get("command") or "") for h in _hooks]
    check("...and every hook command is anchored to $CLAUDE_PROJECT_DIR, so it resolves in the "
          "checkout that is running rather than the one that wrote it",
          all("CLAUDE_PROJECT_DIR" in c for c in _cmds))
    check("...and the scan EXAMINED something — an empty hook list would satisfy both checks above "
          "while looking at nothing, which is the pass-by-silence this whole suite is about",
          len(_cmds) >= 3)

    print("watchdog: the STOP seam is readable too — blocked looks like working from outside:")
    # A launched Crawler's stop-gate refused a turn-end, correctly, and then sat inert for 44
    # minutes: 29s of CPU over 48, hook polling and nothing else. Live pid, open leaf, exit 0,
    # watchdog quiet, every artifact on disk looking like success. It took a chat message to wake it.
    #
    # THE HARNESS KNEW THE WHOLE TIME. stop_gate_blocks_total and stop_triggers.<name> were in
    # session state; nothing read them out, so a consumer had to parse my internals -- the coupling
    # every --porcelain here exists to end. Same shape as #67, one moment over.
    sw = make_sandbox()
    _clean = json.loads(gl(sw, "watchdog", "--porcelain", sid="sess-sg").stdout)
    check("a session that has never been blocked reports blocked:false — the paired arm, or a seam "
          "that always says blocked would be as useless as one that never does",
          _clean["stop_gate"]["blocked"] is False)
    # The session dir is created by a verb that WRITES state; the read-only ones above do not make
    # one, so build the fixture rather than assume a previous call left it.
    _sd = os.path.join(sw, ".game_loop", "sessions", "sess-sg")
    os.makedirs(_sd, exist_ok=True)
    _st = os.path.join(_sd, "state.json")
    try:
        with open(_st) as f:
            _d = json.load(f)
    except (OSError, ValueError):
        _d = {}
    _d["stop_gate_blocks_total"] = 1
    _d["stop_triggers"] = {"showrunner-stop-gate": {"verdict": "blocked", "consecutive": 1,
                                                    "at": "2026-08-14T11:35:36"}}
    with open(_st, "w") as f:
        json.dump(_d, f)
    _blocked = json.loads(gl(sw, "watchdog", "--porcelain", sid="sess-sg").stdout)["stop_gate"]
    check("...and the state a real blocked Crawler carried reports BLOCKED, naming the attachment "
          "and when — which is what a consumer needs to stop calling it legitimately waiting",
          _blocked["blocked"] is True
          and _blocked["attachments"]["showrunner-stop-gate"]["at"] == "2026-08-14T11:35:36"
          and _blocked["attachments"]["showrunner-stop-gate"]["consecutive"] == 1)
    check("...and it says what the stand-down bound actually covers: REPEATED blocking, never a "
          "single block that leaves a session inert — a reader would otherwise assume it bounds "
          "how long a session can be stuck, and it does not",
          "does NOT bound a single block" in _blocked["bound_covers"])
    _human = gl(sw, "watchdog", sid="sess-sg").stdout
    check("...and the human rendering says a blocked session looks identical to a working one from "
          "outside, since that is the property that cost 44 minutes",
          "BLOCKED at turn-end" in _human and "looks exactly like a working one" in _human)

    print("watchdog: the waiting seam is READABLE without parsing a rule file (#67):")
    # An orchestrator is the only party that can answer "is this run waiting on work it dispatched",
    # and it had no way to check whether the seam was armed except parsing config.json -- the exact
    # coupling owned --porcelain and worktree --porcelain exist to end. It was also WRITING that
    # file, which is a rule file byte-compared between a parent checkout and its worktrees, so the
    # layer above was authoring rules it does not own.
    #
    # READ ONLY, AND THE REFUSAL TO ADD A SETTER IS THE MORE IMPORTANT HALF. waiting_probe is
    # config-only because a wait a session can declare for itself is an off switch for the watchdog.
    # A verb that sets it is callable by the agent being watched: `--waiting-probe true` is a probe
    # that always exits 0, which is "always waiting", which is the watchdog disarmed by the thing it
    # watches. The orchestrator really is a different process -- and the verb cannot tell.
    wdp = make_sandbox()
    _bare = gl(wdp, "watchdog")
    check("with no probe configured it says so, and names the file to arm it in — not config.json, "
          "which is byte-compared at spawn and would report as drift",
          "NOT CONFIGURED" in _bare.stdout and 'config.local.json' in _bare.stdout)
    check("...and it states there is NO setter and why, so the next reader does not file the same "
          "request — a verb that cannot identify its caller must assume the worst one",
          "no verb that sets it" in _bare.stdout and "off switch" in _bare.stdout)
    _bj = json.loads(gl(wdp, "watchdog", "--porcelain").stdout)
    check("...and the porcelain says configured:false with a last verdict of None — 'never ran' is "
          "not the same as a verdict of 'not waiting', which is the distinction this seam is for",
          _bj["configured"] is False and _bj["last"] is None and _bj["armed_in"] is None)
    # ARMED FROM THE FILE THE ISSUE SHOULD HAVE USED, which is the whole answer to its write half.
    with open(os.path.join(wdp, ".game_loop", 'config.local.json'), "w") as f:
        json.dump({"watchdog": {"waiting_probe": "exit 1"}}, f)
    _armed = json.loads(gl(wdp, "watchdog", "--porcelain").stdout)
    check("...and arming it in the LOCAL config is seen, and reported as the file that carries it, "
          "so a layer above can set this without touching a rule file at all",
          _armed["configured"] is True and _armed["armed_in"] == 'config.local.json'
          and _armed["command"] == "exit 1")

    print("a BRIEF is not a human — the hatch cannot be spent on your own prompt (#68):")
    # An unattended Crawler hit the MCP guard, could not reach a human, and spent the hatch on the
    # text that had told it what to do: reason="brief instructs: join ... before starting". Twice,
    # in one run. The reading is DEFENSIBLE -- from inside that session the brief IS the instruction
    # from its principal, the only voice it hears, arriving exactly as a human prompt arrives -- and
    # the log still ends up carrying two authorizations that read as human-sanctioned. The whole
    # value of single-use-and-logged is that the log means something later.
    #
    # A NARROW NAMED CLASS, not a prose scan. The rule here is that text detectors nudge and flags
    # enforce; the exception is the agent citing ITSELF, which no human phrasing needs.
    hw = make_sandbox()
    _brief = gl(hw, "authorize", "--path", "/tmp/x",
                "--reason", "brief instructs: join the room before starting")
    check("the reason that was actually recorded in the wild is REFUSED — a brief is text another "
          "agent wrote, arriving the way a human's words arrive",
          _brief.returncode != 0 and "not a human" in (_brief.stdout + _brief.stderr))
    check("...and the refusal names the two real routes — ask via `arm`, or report upward if you "
          "were dispatched — because a refusal with no route is what produced the substitution",
          "arm --question" in (_brief.stdout + _brief.stderr)
          and "upward" in (_brief.stdout + _brief.stderr))
    _human = gl(hw, "authorize", "--path", "/tmp/x",
                "--reason", "Jonah said go ahead and upgrade the repos with no agent")
    check("...while a human's actual sentence still authorises, so the check discriminates rather "
          "than refusing everything — a hatch nobody can spend is a hatch that does not exist",
          _human.returncode == 0 and "AUTHORIZED" in _human.stdout)
    check("...and the record says NO ARMED QUESTION was raised, so a hatch spent with nobody asked "
          "and one spent after asking stop reading the same later",
          "NO armed question" in _human.stdout)

    print("the project's POLICY is not the session's to edit (#65):")
    # A stuck unattended agent added ten MCP verbs to mcp_read_only_tools and a whole-server prefix
    # to mcp_standing_writes, to unblock a call the guard had refused, and reported the edit in its
    # own summary as a fix it had applied. The rule against it was a sentence in llms.txt -- "You
    # cannot set any of them; they are the project's policy, not yours" -- which is INV1's own
    # example of what does not hold. If the agent ignored every instruction, would this? It did not.
    #
    # The edit was DEFENSIBLE, which is the whole difficulty: join and say are innocuous, the project
    # owns that server, and a human reading the diff alone would likely approve it.
    pol = make_sandbox()
    _guard = os.path.join(pol, '.game_loop', "bin", "guard-writes-impl.sh")

    def _edit(path):
        r = subprocess.run(["bash", _guard],
                           input=json.dumps({"tool_name": "Edit",
                                             "tool_input": {"file_path": path}}),
                           capture_output=True, text=True,
                           env=dict(_env(pol), CLAUDE_PROJECT_DIR=pol,
                                    GAME_LOOP_HOME=os.path.join(pol, '.game_loop')))
        return "deny" in (r.stdout or "")

    _cfg = os.path.join(pol, '.game_loop', "config.json")
    check("editing the project's own config.json is REFUSED — the file that decides what a session "
          "may do cannot be the session's to widen, or it is a suggestion rather than a guardrail",
          _edit(_cfg))
    check("...and so is INVARIANTS.md, since the north star governs the same session that would "
          "rewrite it",
          _edit(os.path.join(pol, '.game_loop', "INVARIANTS.md")))
    # NOTES ARE NOT POLICY. Denying an ordinary working file trains people to reach for the escape
    # routinely, which is how an escape stops being exceptional.
    check("...while LEDGER.md is NOTES and stays writable, so the gate does not fire on the ordinary "
          "course of work",
          not _edit(os.path.join(pol, '.game_loop', "LEDGER.md")))
    check("...and an ordinary file in the repo is untouched by this gate",
          not _edit(os.path.join(pol, "README.md")))
    # THE PROVISIONING CARVE-OUT, and it exists because a consumer was ASKED rather than shipped to.
    # An orchestrator provisions worktrees by running install.sh, which SEEDS these files when
    # absent; a blanket deny would have broken that fan-out and I would have found out by breaking
    # it. Creating an absent policy file is provisioning; editing one that is there is the session
    # rewriting its own gate.
    _absent = os.path.join(pol, '.game_loop', "verify.yaml")
    os.remove(_absent)
    check("...and SEEDING a policy file that does not exist yet is allowed — provisioning creates, a "
          "session edits, and the file's existence is what tells those apart with no flag needed",
          not _edit(_absent))

    # THE SECOND DOOR, found by a consumer verifying the first before depending on it. config.local.json is
    # merged with UNION semantics for read_roots, allow_write_roots, deploy_verbs and every mcp_*
    # grant -- additive, and NOT narrowable by the project's own config. Denying config.json alone
    # left a session able to widen its own effective policy through the file I had just recommended
    # to that same consumer for arming their waiting probe.
    #
    # DENIED WHETHER OR NOT IT EXISTS, unlike the rule files: nothing seeds it, so there is no
    # provisioning arm to protect, and creating one is the same act as editing one.
    _loc = os.path.join(pol, ".game_loop", "config" + ".local.json")
    check("the local override is a policy file too — a session cannot widen allow_write_roots or an "
          "mcp_* grant through a file the rule-file deny does not name",
          _edit(_loc))
    with open(_loc, "w") as f:
        json.dump({"watchdog": {"waiting_probe": "exit 1"}}, f)
    check("...and it stays denied once it EXISTS, so the exists-check that separates provisioning "
          "from self-edit on rule files does not become a way in here",
          _edit(_loc))


    print("worktree --porcelain: a script it could NOT compare is undetermined, never clean (#66):")
    # rules and owned each had an `unreadable` branch; `code` never got one when the comparison was
    # widened to include scripts. So a script that could not be READ fell through every test onto
    # `clean` -- exit 0, with a detail sentence claiming every harness script is byte-identical,
    # about a file the verb never opened.
    #
    # NOT A BRIEF-ONLY BLAST RADIUS. The orchestrator that reported this refuses to integrate on
    # drifted-or-undetermined, so a false clean walked through the one gate standing between a
    # drifted Crawler's branch and trunk.
    #
    # ITS OWN FIXTURE, deliberately. The shared worktree fixture above accumulates a LEDGER.md drift
    # and a rule drift by the time this question can be asked, so the verdict returns `notes-drifted`
    # and never reaches the branch under test -- and resetting it clobbers a later assertion. A test
    # that must undo three earlier mutations to ask its question is in the wrong place.
    u_main = mkproject("u_main")
    u_wt = worktree_of(u_main, "u_wt")
    try:
        install(u_wt)                 # a worktree inherits no harness; provisioning is the fixture
        _clean = gl(u_wt, "worktree", "--porcelain")
        check("the paired arm FIRST: an untouched worktree is clean at exit 0, so the refusal below "
              "is a discrimination rather than a verb that always refuses",
              json.loads(_clean.stdout)["status"] == "clean" and _clean.returncode == 0)
        _ps = os.path.join(u_main, '.game_loop/bin', "check-project-thing")
        with open(_ps, "w") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        r = gl(u_wt, "worktree", "--porcelain")
        d = json.loads(r.stdout)
        check("...and a harness script present in the main checkout and ABSENT here is UNDETERMINED "
              "— a script this verb never opened cannot be reported as identical",
              d["status"] == "unreadable"
              and "check-project-thing" in d["code"]["unreadable"])
        check("...and it exits 2, so 'could not tell' stops sharing an exit code with 'nothing to "
              "report' — the property that makes asking better than guessing",
              r.returncode == 2)
        check("...and it flags that an older game_loop called THIS tree clean, because a consumer "
              "cannot re-derive that after the merge it already allowed",
              d.get("false_clean_before_fix") is True
              and "reported THIS tree as clean" in d["detail"])
    finally:
        shutil.rmtree(u_main, ignore_errors=True)

    print("install.sh: the piped one-liner upgrades, not only installs fresh:")
    inst = os.path.join(REPO, "install.sh")
    ibug = tempfile.mkdtemp(prefix="gameloop-install-")
    try:
        def piped(target):
            """`curl | bash` faithfully: bash reads the script from STDIN, so BASH_SOURCE[0] is
            empty — the condition the bug turned on. GAME_LOOP_REPO names a repo that cannot
            resolve so the fetch fails fast; every assertion here is about WHICH BRANCH was taken
            and is printed BEFORE curl runs, so none of them depends on the network."""
            e = _env(); e["GAME_LOOP_REPO"] = "SupposedlySam/no-such-repo-control"
            with open(inst) as f:
                return subprocess.run(["bash", "-s", "--", target], stdin=f,
                                      capture_output=True, text=True, env=e)

        fresh = os.path.join(ibug, "fresh")
        os.makedirs(fresh)
        r = piped(fresh)
        check("piped into a FRESH directory still fetches (the arm that always worked)",
              "Fetching game_loop" in r.stdout)

        upg = os.path.join(ibug, "upgrade", ".game_loop", "bin")
        os.makedirs(upg)
        with open(os.path.join(upg, "game_loop"), "w") as f:
            f.write("#!/usr/bin/env python3\n")
        target = os.path.dirname(os.path.dirname(upg))
        r = piped(target)
        check("piped into a repo that ALREADY has game_loop fetches too — the upgrade path",
              "Fetching game_loop" in r.stdout)
        check("...and never misreads the target's own copy as the payload directory",
              "same directory" not in r.stderr and "repo itself" not in r.stderr)

        # The local-payload arm must not regress into always fetching: run from a real checkout,
        # the script sits next to the payload and the network must not be touched at all.
        local_t = os.path.join(ibug, "local")
        os.makedirs(local_t)
        r = subprocess.run([inst, local_t], capture_output=True, text=True, env=_env())
        check("run from a checkout, the local payload is used and nothing is fetched",
              "Fetching game_loop" not in r.stdout and "Installing game_loop into" in r.stdout)

        r = subprocess.run([inst, REPO], capture_output=True, text=True, env=_env())
        check("installing a checkout onto itself is refused, naming the evidence not an inference",
              r.returncode != 0 and "same directory" in r.stderr and REPO in r.stderr)
    finally:
        shutil.rmtree(ibug, ignore_errors=True)

    # #42: the mutation sweep is the thing that decides whether the checks above are worth anything,
    # so it gets checked here rather than trusted. It is far too slow to RUN from the suite (one
    # unmutated pass plus one per producer), and it does not need to be: what can rot is its
    # verdict line and its list of targets, and both are readable without running it.
    # THE HOUSE VOICE (#33). This repo bans one word, for the Dungeon-Crawler-Carl theme. Until now
    # the rule lived ONLY in a gitignored scratch file, which is to say it did not exist: it reached
    # nobody who cloned the repo, and the tool's own output broke it in four places — including the
    # success line of `harden`, the verb whose whole purpose is converting a learning into something
    # enforced rather than remembered. A rule held up by whoever happens to be reviewing is the
    # exact shape INV1 exists to refuse.
    #
    # The needle is BUILT rather than written, so this file does not contain the thing it forbids
    # and cannot flag itself — the same trick mutation_sweep.py uses for the here-doc operator.
    print("the python EMBEDDED in the shell guards parses:")
    # `bash -n` is what a shell file gets checked with, and it cannot see inside a heredoc or a
    # `python3 -c '...'` block -- to bash those are opaque strings. So a guard whose Python is
    # syntactically broken passes the only syntax check it has, and the breakage surfaces as the
    # guard CRASHING on a live call. Measured here: #57's refusal text was written through a
    # generating script where `\n` escaped one layer early, producing real newlines inside a string
    # literal. `bash -n` passed. The MCP guard then died on every call, and a crashing guard reads
    # as ALLOW to the harness that invokes it -- fail-closed defeated by a syntax error.
    #
    # The existing rule was "ast.parse before you write a .py file". This is the same rule, aimed at
    # where the code actually lives rather than at the extension it happens to have.

    def _embedded_python(text):
        """(label, source) for each Python program embedded in a shell file.

        Shell single-quoting cannot contain a `'`, so the non-greedy match on `-c '...'` is exact
        rather than approximate -- the terminator genuinely cannot appear inside the body.
        WHAT THIS DOES NOT SEE: a program built by string concatenation, one piped in from
        elsewhere, or a heredoc whose terminator is not quoted (which the shell would expand first,
        making it a different program than the one on disk anyway).
        """
        lines = text.split("\n")
        # A shell COMMENT is prose, not code, and this file's own comments quote `python3 -c
        # 'os.remove(..)'` as an example of what the guard cannot see. A check that fires on prose
        # describing code gets narrowed until it fires on nothing, so it declines here instead.
        _comment_at = set()
        _off = 0
        for _ln in lines:
            if _ln.lstrip().startswith("#"):
                _comment_at.update(range(_off, _off + len(_ln) + 1))
            _off += len(_ln) + 1
        found = []
        for m in re.finditer(r"python3 -c '(.*?)'", text, re.S):
            if m.start() in _comment_at:
                continue
            found.append((f"-c at offset {m.start()}", m.group(1)))
        i = 0
        while i < len(lines):
            m = re.search(r"<<'([A-Za-z_][A-Za-z0-9_]*)'", lines[i])
            if m and "python3" in lines[i]:
                term, body, j = m.group(1), [], i + 1
                while j < len(lines) and lines[j] != term:
                    body.append(lines[j])
                    j += 1
                found.append((f"heredoc <<{term} at line {i + 1}", "\n".join(body)))
                i = j
            i += 1
        return found

    def _parses(src):
        try:
            ast.parse(src)
            return True
        except SyntaxError:
            return False

    _broken = "import os\nx = ('a\nb')\n"
    check("the extractor finds a heredoc program AND a -c program, and a deliberately broken one "
          "fails to parse — the check can fire",
          len(_embedded_python("foo | python3 -c 'import os'\nbar python3 <<'PY'\nimport sys\nPY\n"))
          == 2 and not _parses(_broken))

    _bindir = os.path.join(SRC_GAME_LOOP, "bin")
    _shells = sorted(os.path.join(_bindir, n) for n in os.listdir(_bindir) if n.endswith(".sh"))
    check("...and there ARE shell guards to check, so a green result is not an empty loop",
          len(_shells) >= 2)
    _bad = []
    _seen = 0
    for _sh in _shells:
        with open(_sh) as f:
            _txt = f.read()
        for _label, _src in _embedded_python(_txt):
            _seen += 1
            if not _parses(_src):
                _bad.append(os.path.basename(_sh) + " " + _label)
    check(f"every python program embedded in a shell guard parses ({_seen} found) — the check bash "
          "cannot perform on the code it is carrying"
          + (" · BROKEN: " + "; ".join(_bad[:4]) if _bad else ""),
          not _bad)

    print("one terminal session arms the limit gate for every session in the checkout:")
    # THE SNAPSHOT IS ACCOUNT-SCOPED AND FILE-BACKED, so the host that CANNOT produce it can still
    # consume one somebody else produced. That makes the whole limit family reachable from an
    # editor-embedded session — which is the default host for at least one heavy user — without any
    # spawning, PTY driving, or probe that burns an API turn to measure API turns.
    #
    # Asserted because it is now a documented remedy: a user asked whether game_loop could spawn
    # sessions to sample usage, and this is what I recommended instead. A recommendation nothing
    # checks is a claim about my own product with the same standing as prose.
    lw = make_sandbox()
    try:
        def _vs(*a):
            # gl() takes no extra environment, and the entrypoint is the whole point here.
            return subprocess.run([os.path.join(lw, ".game_loop", "bin", "game_loop"), *a],
                                  capture_output=True, text=True,
                                  env=_env(lw, sid="sess-lim",
                                           CLAUDE_CODE_ENTRYPOINT="claude-vscode"))

        check("with no snapshot an editor-hosted session says the protection is INERT — absence of "
              "signal, never evidence of headroom",
              "USAGE-LIMIT PROTECTION IS INERT" in _vs("status").stdout)
        _lf = os.path.join(lw, ".game_loop", "limits.json")
        with open(_lf, "w") as f:
            json.dump({"t": "now", "windows": {"five_hour": {"used_percentage": 99.2,
                                                             "resets_at": int(time.time()) + 3600}}}, f)
        check("...and a snapshot written by a TERMINAL session in the same checkout clears it — the "
              "host that cannot produce one can still consume one",
              "USAGE-LIMIT PROTECTION IS INERT" not in _vs("status").stdout)
        _r = subprocess.run([os.path.join(lw, ".game_loop", "bin", "game_loop"), "limitgate"],
                            input=json.dumps({"tool_name": "Bash", "session_id": "sess-lim",
                                              "tool_input": {"command": "echo hi"}}),
                            capture_output=True, text=True,
                            env=_env(lw, sid="sess-lim", CLAUDE_CODE_ENTRYPOINT="claude-vscode"))
        check("...and the GATE itself arms for that editor-hosted session, which is the half that "
              "matters — a quiet warning is not protection",
              "\"permissionDecision\": \"deny\"" in _r.stdout.replace("'", '"')
              or '"permissionDecision":"deny"' in _r.stdout.replace(" ", ""))
        # PAIRED: a window that has already reset must NOT bind, or a stale snapshot from a terminal
        # session last week would block every tool call today.
        with open(_lf, "w") as f:
            json.dump({"t": "old", "windows": {"five_hour": {"used_percentage": 99.2,
                                                             "resets_at": int(time.time()) - 60}}}, f)
        _r2 = subprocess.run([os.path.join(lw, ".game_loop", "bin", "game_loop"), "limitgate"],
                             input=json.dumps({"tool_name": "Bash", "session_id": "sess-lim",
                                               "tool_input": {"command": "echo hi"}}),
                             capture_output=True, text=True,
                             env=_env(lw, sid="sess-lim", CLAUDE_CODE_ENTRYPOINT="claude-vscode"))
        check("...while a window that already RESET does not bind, so a stale snapshot from last "
              "week cannot block today's work",
              "deny" not in _r2.stdout)
    finally:
        shutil.rmtree(lw, ignore_errors=True)

    print("a session's MODEL is a sequence, readable on any host, published for the parent:")
    # NO HOOK PAYLOAD CARRIES THE MODEL — verified against a real captured Stop payload: cwd,
    # effort, permission_mode, session_id, transcript_path, and no model anywhere. The statusline
    # payload has it and is TUI-only, so an editor-hosted session never sees it. The transcript
    # records it per assistant message and every hook payload points at the transcript.
    mw = make_sandbox()
    try:
        _mt = os.path.join(mw, "transcript.jsonl")

        def _tr(*models):
            with open(_mt, "w") as f:
                for m in models:
                    f.write(json.dumps({"type": "assistant",
                                        "message": {"model": m, "content": []}}) + "\n")

        _mld = importlib.machinery.SourceFileLoader(
            "gl_m", os.path.join(mw, ".game_loop", "bin", "game_loop"))
        _g = importlib.util.module_from_spec(importlib.util.spec_from_loader("gl_m", _mld))
        _mld.exec_module(_g)

        _tr("claude-opus-5")
        check("the model is read from the transcript, so no statusline is required and an "
              "editor-hosted session is not blind",
              (_g.session_models(_mt) or {}).get("last") == "claude-opus-5")

        # A SEQUENCE, NOT A SCALAR, and this is the correction that matters. `--fallback-model`
        # does not fail at spawn — it degrades MID-RUN under load, silently. A single value reports
        # whichever message happened to be read and cannot represent "started as Sonnet, fell back
        # to Haiku at message 400", which is the shape most likely on a real fan-out when every
        # Crawler hits the same limit at once. Raised by showrunner against my first design.
        _tr("claude-sonnet-5", "claude-sonnet-5", "claude-haiku-4-5")
        _v = _g.session_models(_mt)
        check("a model that CHANGED mid-run is reported as the whole sequence — a scalar would "
              "report whichever message was read and hide the fallback entirely",
              _v["models"] == ["claude-sonnet-5", "claude-haiku-4-5"]
              and _v["first"] == "claude-sonnet-5" and _v["last"] == "claude-haiku-4-5"
              and _v["changed"] is True)
        # PAIRED: an unchanged run must NOT report a change, or the flag is noise.
        _tr("claude-sonnet-5", "claude-sonnet-5")
        check("...while a run that never changed model reports changed:false, so the flag still "
              "means something when it fires",
              _g.session_models(_mt)["changed"] is False)
        _tr("claude-opus-5", "claude-sonnet-5", "claude-opus-5")
        check("...and a model that flips BACK still counts as changed, since the run was priced on "
              "both and the question is what happened, not where it ended",
              _g.session_models(_mt)["changed"] is True)
        _tr("claude-sonnet-5", "<synthetic>")
        check("...and a <synthetic> record is skipped — that is the harness talking, not a model "
              "answering, and counting it would report the harness to itself",
              _g.session_models(_mt)["models"] == ["claude-sonnet-5"])
        check("...and an absent transcript reports NOTHING rather than guessing, because absence "
              "of the reading is not evidence of a model",
              _g.session_models(os.path.join(mw, "nope.jsonl")) is None)

        # THE READER IS NOT THIS SESSION. A status line about a model mismatch is read by the
        # Crawler — the one party that cannot act on it, since it cannot switch its own model and
        # the run is already priced by the time it reads. The party that CAN act is the
        # orchestrator, a different process, outside. So the verdict is a file keyed by session id.
        _tr("claude-sonnet-5", "claude-haiku-4-5")
        # The verb reads the path from SESSION STATE, because that is where a hook records it —
        # `model` is not a hook and never receives a payload of its own.
        _msd = os.path.join(mw, ".game_loop", "sessions", "sess-model")
        os.makedirs(_msd, exist_ok=True)
        with open(os.path.join(_msd, "state.json"), "w") as f:
            json.dump({"transcript_path": _mt}, f)
        _mj = gl(mw, "model", "--json", sid="sess-model")
        check("the verdict is machine-readable and keyed by SESSION, because the party that can "
              "act on a mismatch is the parent that dispatched it, not the session reading a line",
              _mj.returncode == 0 and json.loads(_mj.stdout)["session"] == "sess-model")
        _vf = os.path.join(mw, ".game_loop", "sessions", "sess-model", "model.json")
        check("...and it is written to disk where a parent can read it WITHOUT the session "
              "cooperating or even knowing",
              os.path.isfile(_vf) and json.load(open(_vf))["changed"] is True)

        # ADVISORY, NEVER A GATE: a model mismatch is a COST failure, not a correctness one. The
        # output is fine, which is exactly why nothing notices and why it can run for a week.
        _rep = "\n".join(_g.model_report({"transcript_path": _mt}))
        check("a mid-run model change is reported LOUDLY and explicitly not blocked — bricking a "
              "deliberately-dispatched Crawler would do more damage than the thing it guards",
              "MODEL CHANGED MID-RUN" in _rep and "Not blocked" in _rep and "COST failure" in _rep)
        check("...and NOTHING has to be declared for that to work, which is what makes the feature "
              "optional by construction rather than by a config default",
              "--model" not in _rep and "config" not in _rep)
    finally:
        shutil.rmtree(mw, ignore_errors=True)

    print("the account-scoped snapshot is shared across a project's worktrees:")
    # THE RESOURCE IS ACCOUNT-WIDE AND THE FILE WAS PER-CHECKOUT. #47 settled that a linked worktree
    # IS this project — for the write guard — and that rule never reached the limits snapshot. Every
    # worktree resolved it under its own ROOT, and it is gitignored so it does not cross, so N
    # worktrees meant N snapshots of one account's windows, N probes fetching the same number, and
    # N leases each believing it was alone. Found by an orchestrator reading this source against its
    # own dispatch topology, after I told it the lease covered its fan-out.
    lw2 = make_sandbox()
    try:
        _ld2 = importlib.machinery.SourceFileLoader(
            "gl_lim", os.path.join(lw2, ".game_loop", "bin", "game_loop"))
        _gl2 = importlib.util.module_from_spec(importlib.util.spec_from_loader("gl_lim", _ld2))
        _ld2.exec_module(_gl2)

        _main = os.path.join(lw2, "mainco")
        os.makedirs(os.path.join(_main, ".game_loop"), exist_ok=True)
        _gl2.main_checkout = lambda: _main
        _gl2._LIMITS_PATH = None
        check("from a linked worktree the snapshot resolves to the MAIN checkout — one account, one "
              "file, so the lease and the reading are shared rather than duplicated per tree",
              _gl2.limits_file() == os.path.join(_main, ".game_loop", "limits.json"))
        # PAIRED: the ordinary case must be unchanged, or this would relocate every solo install.
        _gl2.main_checkout = lambda: None
        _gl2._LIMITS_PATH = None
        check("...while a repo that is NOT a worktree keeps its own, so the fix relocates nothing "
              "for the ordinary single-checkout install",
              _gl2.limits_file() == _gl2._LIMITS_LOCAL)
        # DEGRADES, NEVER RAISES: this runs inside a statusline refresh and a Stop hook.
        def _boom():
            raise RuntimeError("git said no")
        _gl2.main_checkout = _boom
        _gl2._LIMITS_PATH = None
        check("...and a git that cannot answer degrades to this tree rather than raising — a "
              "snapshot in the wrong place is a degraded reading, a raise takes the hook down",
              _gl2.limits_file() == _gl2._LIMITS_LOCAL)

        # ONLY THE SNAPSHOT MOVES. Sharing session state, the edited set, claims or authorizations
        # would collapse the isolation the worktree exists for.
        _src = open(os.path.join(SRC_GAME_LOOP, "bin", "game_loop")).read()
        check("...and ONLY the snapshot moved: state, claims and authorizations still resolve under "
              "this tree's own ROOT, which is the isolation a worktree is for",
              'STATE_F = ' in _src and "os.path.join(ROOT" in _src)
        check("...and the lock moved WITH the data file, since a shared file with per-tree locks "
              "races worse than the private files it replaced",
              'os.path.dirname(limits_file()), ".limits.lock"' in _src)
        # A FIX THAT READS AS UNFIXED IS A DEFECT — the consumer greps, and the grep must not lie.
        # showrunner nearly filed a false report off the old constant, which sat three lines above
        # the resolver still carrying the word "account-scoped" on a per-checkout expression.
        check("...and the per-checkout path is named as the FALLBACK it is, so grepping the snapshot "
              "cannot land on an expression that reads like the unfixed version",
              "LIMITS_F" not in _src and "_LIMITS_LOCAL = os.path.join(ROOT" in _src)
        check("...and nothing reads that fallback directly — it is referenced ONCE, inside the "
              "resolver, so a new direct reader reintroduces the per-checkout snapshot loudly",
              _src.count("_LIMITS_LOCAL") == 2)
        _wd = open(os.path.join(SRC_GAME_LOOP, "bin", "watchdog")).read()
        check("...and the watchdog's resolved path no longer shares a NAME with that fallback, "
              "since one identifier meaning both things across two files is what misread once",
              "LIMITS_F" not in _wd and "LIMITS_PATH = _limits_file()" in _wd)
    finally:
        shutil.rmtree(lw2, ignore_errors=True)

    print("the limit probe: a reading on a host that renders no statusline:")
    # THE COST IS REAL AND SO IS THE ALTERNATIVE. ~24k input tokens per run, measured, against an
    # unattended run that hits its limit at 1am, dies mid-action, and is still dead at 7am because
    # nothing could see the wall coming. The whole limit family is built and tested and was only
    # ever missing the snapshot. I first argued against this by comparing the probe's cost to ZERO,
    # which is not the alternative.
    pw2 = make_sandbox()
    try:
        check("the probe ships as executable code beside the other scripts, so a pinned harness "
              "runs the pinned probe",
              os.access(os.path.join(SRC_GAME_LOOP, "bin", "limit-probe.sh"), os.X_OK))

        # A SECOND READING RIDING THE SAME SPAWN. The probe's session is FRESH, and 2.1.223's payload
        # schema carries total_input_tokens — so the token-floor claim can be exercised on a spawn
        # already being paid for instead of buying one. What is NOT known is whether a fresh render
        # carries a usable count or a null current_usage, and the only way to find out is to run a
        # probe. So this CAPTURES and asserts nothing about meaning.
        _ldp = importlib.machinery.SourceFileLoader(
            "gl_probe", os.path.join(pw2, ".game_loop", "bin", "game_loop"))
        _glp = importlib.util.module_from_spec(importlib.util.spec_from_loader("gl_probe", _ldp))
        _ldp.exec_module(_glp)
        # NOT named _env: this suite already has an _env() helper that closures capture, and
        # shadowing it broke them from inside a block that never mentions it.
        _envelope = {"rate_limits": {"five_hour": {"used_percentage": 7}},
                     "context_window": {"total_input_tokens": 31547, "context_window_size": 200000}}
        check("the envelope yields BOTH readings, so the usage windows are unchanged and the token "
              "count rides beside them rather than replacing them",
              _glp.probe_reading(_envelope) ==
              ({"five_hour": {"used_percentage": 7}},
               {"total_input_tokens": 31547, "context_window_size": 200000}))
        # THE PINNED-OLDER-PROBE ARM. The pinned copy and the repo copy are separate trees; assuming
        # they move together is the assumption this suite exists to refuse.
        check("...and the OLDER bare shape still reads as the windows it always was, because a pin "
              "can pair a shipped older probe script with newer code",
              _glp.probe_reading({"five_hour": {"used_percentage": 7}}) ==
              ({"five_hour": {"used_percentage": 7}}, None))
        check("...and an envelope whose context_window is null yields the windows and an explicit "
              "None, so a missing second reading cannot be mistaken for a zero one",
              _glp.probe_reading({"rate_limits": {"seven_day": {}}, "context_window": None}) ==
              ({"seven_day": {}}, None))
        check("...and a probe that answered with something that is not an object at all yields "
              "nothing rather than raising inside the verb that spent the tokens",
              _glp.probe_reading("nonsense") == ({}, None) and
              _glp.probe_reading(None) == ({}, None))
        # ABSENCE IS RECORDED, not left as a missing file — a file that is not there cannot say
        # whether the probe ran, which is the three-outcome rule applied to its own artifact.
        _glp.record_context_window(None)
        _cwf = os.path.join(pw2, ".game_loop", "probe", "context-window.json")
        with open(_cwf) as f:
            _cwd = json.load(f)
        check("a render carrying NO context_window is recorded as an explicit null, so 'the probe "
              "ran and saw none' is distinguishable from 'the probe never ran'",
              os.path.exists(_cwf) and _cwd["context_window"] is None and "observed_at" in _cwd)
        _glp.record_context_window({"total_input_tokens": 24377})
        with open(_cwf) as f:
            check("...and a real one is stored RAW and uninterpreted, so the first exercise reads a "
                  "reading rather than somebody's expectation of one",
                  json.load(f)["context_window"] == {"total_input_tokens": 24377})
        _off = gl(pw2, "limitprobe", sid="sess-lp")
        check("it is OFF unless a human turned it on — a verb that spends tokens is a decision "
              "somebody makes, never one they inherit",
              _off.returncode != 0 and "probe is off" in _off.stderr
              and "24k input tokens" in _off.stderr)
        check("...and the refusal names the exact config key and the one-off escape, so being "
              "refused does not mean being stuck",
              '"probe": {"enabled": true}' in _off.stderr and "--force" in _off.stderr)

        # THE INTERVAL SCALES WITH WHAT IS AT STAKE. A fixed one is wrong at both ends: far from the
        # limit it burns tokens to learn nothing, near it, it learns too late.
        _ld = importlib.machinery.SourceFileLoader(
            "gl_lp", os.path.join(pw2, ".game_loop", "bin", "game_loop"))
        _glm = importlib.util.module_from_spec(importlib.util.spec_from_loader("gl_lp", _ld))
        _ld.exec_module(_glm)
        _pc = {"min_interval_sec": 300, "max_interval_sec": 3600}
        _now = time.time()

        def _snap(pct):
            return {"windows": {"five_hour": {"used_percentage": pct, "resets_at": _now + 3600}}}

        _far, _near = (_glm.next_probe_after(_snap(5), _now, _pc),
                       _glm.next_probe_after(_snap(95), _now, _pc))
        check("a nearly-full window is probed sooner than an empty one — the spend rises with the "
              "risk rather than running at a fixed rate",
              _near < _far and _near <= _pc["min_interval_sec"] + 1)
        check("...and with NO snapshot it waits the longest, since the first probe has the least "
              "information and should not also be the most frequent",
              _glm.next_probe_after(None, _now, _pc) == _pc["max_interval_sec"])
        check("...and a window that has already RESET does not shorten anything, because the thing "
              "being timed is when work would stop and a reset window stops nothing",
              _glm.next_probe_after({"windows": {"five_hour": {"used_percentage": 99,
                                                              "resets_at": _now - 10}}},
                                    _now, _pc) == _pc["max_interval_sec"])

        # THE EXERCISE MUST READ THE SHAPE THE SNAPSHOT ACTUALLY HAS. The first version read the
        # windows at the top level, so it could never report the claim confirmed on ANY host — an
        # exercise incapable of firing positive reports "not observed" forever, which reads as the
        # claim failing rather than as the check being wrong. Found only when a real snapshot
        # finally existed to compare against.
        with open(os.path.join(pw2, ".game_loop", "limits.json"), "w") as f:
            json.dump({"captured_at": _now, "windows": {
                "five_hour": {"used_percentage": 10, "resets_at": _now + 3600},
                "seven_day": {"used_percentage": 62, "resets_at": _now + 86400}}}, f)
        _st = gl(pw2, "status", sid="sess-lp").stdout
        check("a real snapshot CONFIRMS the statusline claim live — the exercise reads windows "
              "where they are, not where the first draft guessed",
              "'statusline-rate-limits' CONFIRMED LIVE" in _st)
        # ONE PROBE SERVES THE CHECKOUT. The snapshot is account-scoped, so the answer is the same
        # for every session sharing it — but each runs its own watchdog and each would find it stale
        # and buy the same number. Reported from a checkout with EIGHTEEN concurrent sessions: that
        # is 430k input tokens to read one figure.
        # The lease check sits AFTER the enabled check, so these arms need the probe turned on —
        # otherwise they would pass by dying at "the probe is off" and prove nothing about leases.
        _lpcfg = os.path.join(pw2, ".game_loop", "config.json")
        with open(_lpcfg) as f:
            _lpc = json.load(f)
        _lpc["limits"] = dict(_lpc.get("limits") or {}, probe={"enabled": True})
        with open(_lpcfg, "w") as f:
            json.dump(_lpc, f)
        _lpf = os.path.join(pw2, ".game_loop", "limits.json")
        with open(_lpf, "w") as f:
            json.dump({"captured_at": 0, "windows": {}, "probe_started_at": time.time()}, f)
        _peer = gl(pw2, "limitprobe", sid="sess-lp2")
        check("a session whose PEER just started a probe skips it — one probe serves the checkout, "
              "because the number is account-scoped and so is the answer",
              "skipping" in _peer.stdout and "account-scoped" in _peer.stdout)
        # A LEASE, NOT A LOCK HELD ACROSS THE SPAWN: the probe takes ~75s and the limits lock is
        # what every statusline refresh takes, so holding it would stall the whole checkout.
        _gl_src = open(os.path.join(SRC_GAME_LOOP, "bin", "game_loop")).read()
        check("...and the claim is a LEASE that expires, so a session dying mid-probe costs one "
              "duplicate rather than silencing every other session indefinitely",
              "PROBE_LEASE_SEC = 300" in _gl_src)
        with open(_lpf, "w") as f:
            json.dump({"captured_at": 0, "windows": {},
                       "probe_started_at": time.time() - 600}, f)
        # NOTE: this arm must NOT spawn a real session — the suite would then cost ~24k input
        # tokens per run and fail without credentials. Removing the probe script makes it fail at
        # "script is missing", which is past the lease check and therefore still proves the point.
        os.remove(os.path.join(pw2, ".game_loop", "bin", "limit-probe.sh"))
        _stale = gl(pw2, "limitprobe", sid="sess-lp2")
        check("...and an EXPIRED claim does not defer — the skip above is a live peer, not a "
              "permanent wedge",
              "skipping" not in _stale.stdout and "probe script is missing" in _stale.stderr)

        # THE WATCHDOG MUST ASK THE SAME FUNCTION THE VERB REPORTS FROM, or the two disagree about
        # when the next spend is due and neither is wrong from where it stands.
        _iv = gl(pw2, "limitprobe", "--interval-only", sid="sess-lp")
        check("--interval-only prints seconds and nothing else, so the watchdog and the verb cannot "
              "disagree about when the next probe is due",
              _iv.returncode == 0 and _iv.stdout.strip().isdigit())
        _wd_src = open(os.path.join(SRC_GAME_LOOP, "bin", "watchdog")).read()
        check("...and the watchdog refreshes the snapshot BEFORE deciding whether to park — parking "
              "on a stale reading is the same failure as not parking at all",
              _wd_src.index("refresh_limits_if_due()") < _wd_src.index("park_until_limit_resets(state)  #"))
        check("...and it asks for the interval rather than inventing one, and is OFF unless the "
              "project enabled it",
              "--interval-only" in _wd_src and 'pc.get("enabled")' in _wd_src)
        check("...and a probe that fails cannot take the watchdog with it — a guard whose failure "
              "stops the guard above it is worse than the gap it fills",
              "probe raised" in _wd_src and "never blocking a ring" in _wd_src.lower()
              or "NEVER FATAL" in _wd_src)

        check("...and each exercised claim reports its OWN evidence exactly once, rather than one "
              "claim carrying another's reading under its name",
              _st.count("'no-weekly-opus-window' CONFIRMED") == 1)
    finally:
        shutil.rmtree(pw2, ignore_errors=True)

    print("a claim about the HOST cannot go stale quietly (showrunner's INV20):")
    # A limitation written in prose ages exactly like a premise does, and nothing here noticed. The
    # shape came from showrunner: a claim cannot be checked automatically, but its SUBJECT can be
    # versioned. Deciding whether "rate_limits rides the statusline payload" still holds needs a
    # person; deciding whether the release it was verified against is the release running is cheap.
    with open(os.path.join(SRC_GAME_LOOP, "claims.json")) as f:
        _cl = json.load(f)
    _block = _cl.get("claude_code") or {}
    _claims = _block.get("claims") or []
    check("the load-bearing claims about the host are enumerated with what breaks if each moved",
          len(_claims) >= 5 and all(c.get("id") and c.get("says") and c.get("breaks")
                                    for c in _claims))
    check("...and every one carries the DATE it was checked, so an unbounded duty becomes a dated "
          "one",
          bool(_block.get("verified_on")))

    # DEFAULT-DENY BY IDENTITY, NOT BY COUNT — the part showrunner warned costs something and is
    # also the part without which it lies to you. A count passes when one entry is added and
    # another removed. Matching the exact LEDGER heading means a new claim about the host cannot
    # join quietly. (OPEN is excluded on purpose: those are questions, not claims.)
    with open(os.path.join(SRC_GAME_LOOP, "LEDGER.md")) as f:
        _led = f.read()
    _sec = _led.split("## VERIFIED", 1)[1].split("## OPEN", 1)[0]
    _titles = re.findall(r"^- \*\*(.+?)\*\*", _sec, re.M)
    _accounted = {c.get("ledger") for c in _claims}
    _missing = [t for t in _titles if t not in _accounted]
    check(f"every LEDGER claim about the host is accounted for ({len(_titles)} found) — a new one "
          "fails the run until somebody decides it, rather than joining quietly"
          + (" · UNACCOUNTED: " + "; ".join(_missing[:3]) if _missing else ""),
          _titles and not _missing)
    check("...and the check can FIRE — an invented heading is reported as unaccounted, so the clean "
          "result above is a verdict and not a regex matching nothing",
          [t for t in _titles + ["A claim nobody decided"] if t not in _accounted]
          == ["A claim nobody decided"])

    # AND IT MUST NOT IMPLY IT CHECKED THE TRUTH. verified_against is null on every entry, which is
    # the finding rather than an oversight: the original verification never recorded a version, so
    # there is nothing to compare and the report has to say exactly that.
    # AN EXERCISE OUTRANKS A STAMP (showrunner). A stamp records that the AUTHOR checked something
    # on the AUTHOR's machine and asserts it about a stranger's; an exercise checks it on the
    # stranger's, every run, and has no recorded answer to rot. Paired both ways in a sandbox,
    # because the repo itself runs on a host whose tap never fires — so the un-observed arm here
    # would otherwise be the only one anybody ever saw.
    xw = make_sandbox()
    try:
        _xs = gl(xw, "status", sid="sess-x").stdout
        check("an exercisable claim with no observation says so, and says it cannot tell a tap that "
              "never ran from a claim that stopped holding",
              "has NOT been observed here" in _xs and "cannot tell which" in _xs)
        # NESTED UNDER "windows", the shape the tap and the probe actually write. The first
        # version of these fixtures used the top level and so did the code they exercised —
        # test and product wrong together, agreeing with each other, passing. Only a REAL
        # snapshot from a live probe showed the exercise could never have fired on a real host.
        with open(os.path.join(xw, ".game_loop", "limits.json"), "w") as f:
            json.dump({"windows": {"five_hour": {"used_percentage": 12, "resets_at": 1}}}, f)
        _xs2 = gl(xw, "status", sid="sess-x").stdout
        check("...and a real snapshot in the expected shape reports the claim CONFIRMED LIVE on "
              "this machine — the host's own behaviour, not a record of somebody checking",
              "CONFIRMED LIVE here" in _xs2 and "cannot be stale" in _xs2)
        with open(os.path.join(xw, ".game_loop", "limits.json"), "w") as f:
            json.dump({"windows": {"five_hour": {"nope": 1}}}, f)
        check("...while a snapshot MISSING the field the claim is about does not count as "
              "confirming it — the exercise checks the shape, not the file's existence",
              "has NOT been observed here" in gl(xw, "status", sid="sess-x").stdout)

        # A DECLARED DEBT PAID. This claim carried its own positive control in prose for two weeks
        # while nothing checked it — the shape of every stale claim here: everything needed was
        # written down, only the code was missing. All three arms, because an exercise that cannot
        # report failure is a stamp with extra steps, and one that confirms on nothing is worse.
        _pd = os.path.join(xw, ".game_loop", "probe")
        os.makedirs(_pd, exist_ok=True)
        _cf = os.path.join(_pd, "claims-observed.json")
        with open(_cf, "w") as f:
            json.dump({"control": False, "rate_limit_keys": [], "top_level_keys": []}, f)
        check("a hook payload whose CONTROL did not fire confirms nothing — no session_id means "
              "the read cannot be distinguished from a hook that never ran",
              "'no-rate-limits-in-hooks' is exercisable and unobserved" in
              gl(xw, "status", sid="sess-x").stdout)
        with open(_cf, "w") as f:
            json.dump({"control": True, "rate_limit_keys": [],
                       "top_level_keys": ["cwd", "hook_event_name", "session_id"]}, f)
        check("...and a real payload carrying session_id and NO rate-limit key confirms it live on "
              "this host, which is the claim checked where it is consumed rather than stamped",
              "'no-rate-limits-in-hooks' CONFIRMED LIVE" in gl(xw, "status", sid="sess-x").stdout)
        with open(_cf, "w") as f:
            json.dump({"control": True, "rate_limit_keys": ["rate_limits.five_hour"],
                       "top_level_keys": ["session_id", "rate_limits"]}, f)
        check("...and a payload that DOES carry rate-limit data falsifies it by name — the arm "
              "that would make the whole statusline detour unnecessary",
              "'no-rate-limits-in-hooks' IS NO LONGER TRUE" in gl(xw, "status", sid="sess-x").stdout)
        # NESTED, not just top level: the key that would falsify this claim is the one whose real
        # shape put the windows under "windows", which already fooled one exercise in this file.
        _ldx = importlib.machinery.SourceFileLoader(
            "gl_hook", os.path.join(xw, ".game_loop", "bin", "game_loop"))
        _glx = importlib.util.module_from_spec(importlib.util.spec_from_loader("gl_hook", _ldx))
        _ldx.exec_module(_glx)
        check("the scan finds a rate-limit key NESTED in a payload, not only at the top level — a "
              "shallow scan would report the claim confirmed by a payload that falsifies it",
              _glx._rate_limit_keys({"a": {"b": {"rate_limits": {"five_hour": 1}}}}) ==
              ["a.b.rate_limits"])
        check("...and a payload with no such key anywhere returns nothing, so the scan is not "
              "matching everything it is handed",
              _glx._rate_limit_keys({"session_id": "x", "cwd": "/tmp", "n": [1, 2]}) == [])
        os.remove(_cf)
        # END TO END, because every assertion above this line writes the observation file BY HAND.
        # The recorder could have been broken in production — wrong path, wrong key, never called —
        # and all of them would still pass. Test and product agreeing because the same hand wrote
        # both is the exact failure the top-level-vs-nested snapshot already cost this file once.
        _glx.HOOK_CLAIM_F = _cf   # its ROOT resolved at import, not this sandbox; aim it explicitly
        _glx.record_hook_claim_observation(
            {"session_id": "s1", "hook_event_name": "Stop",
             "deep": {"nested": {"rate_limits": {"five_hour": {"used_percentage": 3}}}}})
        with open(_cf) as f:
            _rec = json.load(f)
        check("the RECORDER writes what the reporter reads, driven with a real payload — the half "
              "that runs in production, which every assertion above tested only through a fixture",
              _rec.get("control") is True and
              _rec.get("rate_limit_keys") == ["deep.nested.rate_limits"])
        check("...and status then falsifies the claim from that RECORDED observation, so both "
              "halves are exercised through the path between them rather than each on its own",
              "'no-rate-limits-in-hooks' IS NO LONGER TRUE" in
              gl(xw, "status", sid="sess-x").stdout)
        _glx.record_hook_claim_observation({"session_id": "s1", "hook_event_name": "Stop"})
        check("...and the ordinary payload — one with no rate-limit key anywhere — records the "
              "control as fired and confirms the claim, which is the arm a real host produces",
              "'no-rate-limits-in-hooks' CONFIRMED LIVE" in
              gl(xw, "status", sid="sess-x").stdout)
        _glx.record_hook_claim_observation("not a payload at all")
        check("...and a payload that is not a dict leaves the last real observation standing "
              "rather than overwriting it with a verdict about nothing",
              "'no-rate-limits-in-hooks' CONFIRMED LIVE" in
              gl(xw, "status", sid="sess-x").stdout)


        # A CONDITIONAL ABSENCE IS EXERCISABLE, which corrects a bound I stated publicly and got
        # wrong. Not presence-versus-absence: whether you CONTROL THE INPUT that would produce the
        # effect. "No third window" is trivially true of an empty file, a missing file and a broken
        # parser — so it means nothing until a KNOWN window is present, which is the mechanism
        # demonstrably firing. That pairing turns an absence into a discrimination.
        _lf = os.path.join(xw, ".game_loop", "limits.json")
        with open(_lf, "w") as f:
            json.dump({}, f)
        check("an absence with no positive control is NOT reported as confirming anything — an "
              "empty snapshot satisfies 'no third window' and proves nothing",
              "positive control did not run" in gl(xw, "status", sid="sess-x").stdout)
        with open(_lf, "w") as f:
            json.dump({"windows": {"five_hour": {"used_percentage": 4},
                                   "seven_day": {"used_percentage": 9}}}, f)
        check("...and with a known window present the absence IS confirmed live, because the read "
              "demonstrably worked and the unexpected window is still not there",
              "'no-weekly-opus-window' CONFIRMED LIVE" in gl(xw, "status", sid="sess-x").stdout)
        # THE ARM THAT MATTERS MOST: it must be able to say the claim STOPPED being true.
        with open(_lf, "w") as f:
            json.dump({"windows": {"five_hour": {"used_percentage": 4},
                                   "weekly_opus": {"used_percentage": 30}}}, f)
        _falsified = gl(xw, "status", sid="sess-x").stdout
        check("...and a window the claim says does not exist FALSIFIES it by name — an exercise "
              "that could never report failure is a stamp with extra steps",
              "IS NO LONGER TRUE" in _falsified and "weekly_opus" in _falsified)
    finally:
        shutil.rmtree(xw, ignore_errors=True)

    # PER-CLAIM VERIFICATION. The block stamp could not express "one of these was re-read", so
    # filling it in would have spoken one re-reading as six — and the file had PROMISED that the
    # first re-verification would fill in the null and make that entry checkable. Unkeepable as
    # written, which is a small instance of exactly what the file exists to catch.
    _cl_claims = _block.get("claims") or []
    check("verification lives on each CLAIM, so a partial re-read cannot be reported as a whole one",
          all("verified_on" in c for c in _cl_claims))
    _redone = [c for c in _cl_claims if c.get("verified_against")]
    check("...and at least one has actually been re-read against a recorded version, so the "
          "mechanism has fired once rather than only being available",
          _redone and all(c.get("verified_how") for c in _redone))
    _cs = gl(REPO, "status").stdout
    check("...and status names the re-read entry AND says how many were not — a leaked loop "
          "variable made the block line report the per-claim date, which is one re-reading "
          "wearing the authority of six",
          "have NOT been re-read" in _cs
          and _block["verified_on"] in _cs and _block["verified_on"] != _redone[0]["verified_on"])

    _rep = "\n".join(gl(REPO, "status").stdout.split("\n"))
    check("status reports the claims, and says plainly that nothing here checks whether one is TRUE",
          "load-bearing claim(s) about" in _rep and "never checks whether a claim is TRUE" in _rep)
    check("...and with no version recorded it says nothing can be compared, rather than reporting "
          "agreement it did not establish",
          "UNRECORDED version" in _rep and "not an assurance" in _rep)

    print("the behaviour record is a contract a consumer can pin to:")
    with open(os.path.join(SRC_GAME_LOOP, "behaviour.json")) as f:
        _bh = json.load(f)
    # A SCHEMA INTEGER, because this file announces changes to game_loop's verbs and nothing
    # announced changes to THIS FILE. Adding a field is safe; renaming `change` or dropping
    # `why_it_qualifies` would break a programmatic reader silently — and there is one now.
    check("the behaviour record declares a schema version, so a consumer pins to the contract "
          "rather than to the shape it happens to see today",
          isinstance(_bh.get("schema"), int) and _bh["schema"] >= 1)
    # EVERY ENTRY CARRIES EVERY FIELD. Adding `consequence` to seq 1 alone reproduced the exact
    # defect the field was added to fix: for the other four, reading `consequence` returned nothing
    # and nothing distinguished "predates the field" from "the author judged there is none". Two
    # causes, one indistinguishable result, and only the author holds the difference.
    _req = ("seq", "sha", "verb", "change", "consequence", "why_it_qualifies")
    _short = [f"seq {e.get('seq')}: missing " + ",".join(k for k in _req if not e.get(k))
              for e in _bh["changes"] if not all(e.get(k) for k in _req)]
    check("every entry carries every field, so an absent consequence cannot mean either 'predates "
          "the field' or 'the author decided there is none'"
          + (" · " + "; ".join(_short[:3]) if _short else ""),
          _bh["changes"] and not _short)
    # An entry complete but for the field this whole change is about — the precise case that was
    # live an hour ago, when seq 1 had a consequence and seq 2-5 did not.
    _synthetic = {"seq": 9, "sha": "abc1234", "verb": "v", "change": "c", "why_it_qualifies": "w"}
    check("...and the check can FIRE, naming the one missing field — so the clean result above is a "
          "verdict rather than a loop over nothing",
          [k for k in _req if not _synthetic.get(k)] == ["consequence"])
    check("...and seq is unique and monotonic, since it is the ordering shas cannot provide",
          [e["seq"] for e in _bh["changes"]] == sorted({e["seq"] for e in _bh["changes"]}))

    print("a change to what game_loop REFUSES cannot go unrecorded (behaviour gate):")
    # THE MECHANISM WAS BUILT AND THE DISCIPLINE LAPSED SILENTLY, which a consumer measured rather
    # than me: twelve commits touched the refusal paths after behaviour.json's first and only entry,
    # and four qualified. The sharpest — a commit target built from a variable, which used to pass
    # SILENTLY and now denies — went unrecorded for weeks, so that consumer described the OLD
    # behaviour to every agent it spawns, in the voice of a measured finding, because re-deriving it
    # from source is what you do when the record is empty.
    bg = tempfile.mkdtemp(prefix="gameloop-bg-")
    try:
        _bgs = os.path.join(bg, ".game_loop", "bin")
        os.makedirs(_bgs)
        _guard = os.path.join(bg, ".game_loop", "bin", "guard-writes-impl.sh")
        _rec = os.path.join(bg, ".game_loop", "behaviour.json")
        with open(_guard, "w") as f:
            f.write('#!/usr/bin/env bash\necho ok\n')
        with open(_rec, "w") as f:
            json.dump({"changes": []}, f)
        shutil.copy(os.path.join(REPO, "test", "behaviour_gate.py"),
                    os.path.join(bg, "behaviour_gate.py"))

        def _git(*a):
            return subprocess.run(["git"] + list(a), cwd=bg, capture_output=True, text=True)

        def _gate():
            return subprocess.run([sys.executable, "behaviour_gate.py", "HEAD"], cwd=bg,
                                  capture_output=True, text=True)

        _git("init", "-q", "-b", "main")
        _git("config", "user.email", "t@t"); _git("config", "user.name", "t")
        _git("add", "-A"); _git("commit", "-qm", "base")

        check("with nothing changed the gate is silent — it must not fire on every commit or it "
              "becomes noise and gets turned off",
              _gate().returncode == 0 and "nothing owed" in _gate().stdout)

        # A REFUSAL CHANGES AND THE RECORD DOES NOT: the case that actually happened.
        with open(_guard, "a") as f:
            f.write('deny "BLOCKED: something new"\n')
        _bad = _gate()
        check("a changed refusal line with no record entry FAILS, naming the lines — an omission "
              "is otherwise indistinguishable from 'nothing changed'",
              _bad.returncode == 1 and "BEHAVIOUR GATE" in _bad.stderr
              and "BLOCKED: something new" in _bad.stderr)
        check("...and the refusal offers BOTH answers, including recording that nobody would "
              "notice — a decision on the record rather than silence",
              '"notice": false' in _bad.stderr and "would somebody who changed NOTHING" in _bad.stderr)

        # PAIRED: touching the record clears it. Otherwise the gate is a wall, not a prompt.
        with open(_rec, "w") as f:
            json.dump({"changes": [{"seq": 1, "change": "x"}]}, f)
        check("...and updating the record in the same change clears it",
              _gate().returncode == 0)

        # AND IT MUST NOT FIRE ON ORDINARY EDITS to the same watched file — a gate that cannot tell
        # a comment from a denial would be met by moving denials somewhere it does not look.
        _git("add", "-A"); _git("commit", "-qm", "recorded")
        with open(_guard, "a") as f:
            f.write('# just a comment, and an echo\necho hello\n')
        check("...while a non-refusal edit to the very same file owes nothing, so the gate stays "
              "aimed at behaviour rather than at activity",
              _gate().returncode == 0 and "nothing owed" in _gate().stdout)
    finally:
        shutil.rmtree(bg, ignore_errors=True)

    # THE SKILLS THIS SHIPS ARE THE ONE THING THE INSTALLER WRITES OUTSIDE THE TARGET — into the
    # user's home, where every project sees them. Two failures are possible there and both are
    # silent: installing them when nobody asked, and destroying a skill of the same name that
    # somebody else wrote. The name collision is the ONLY evidence a hand-written skill ever
    # existed, so a copy over it leaves nothing to notice afterwards. Driven through the real
    # installer's real interface, like every other install test here.
    print("the shipped skills are asked for, and never clobber somebody else's (#skills):")
    _sk_src = os.path.join(REPO, "templates", "skills")
    _sk_names = sorted(d for d in os.listdir(_sk_src)
                       if os.path.isfile(os.path.join(_sk_src, d, "SKILL.md"))) \
        if os.path.isdir(_sk_src) else []
    # A scan over an empty set passes in perfect silence — the same denominator problem the theme
    # scan and the mutation sweep both carry. Say the count is real before trusting what it checks.
    check("there are skills to ship at all, so the checks below have a subject",
          len(_sk_names) >= 2 and "gl-install" in _sk_names)
    _bad_front = []
    for _n in _sk_names:
        with open(os.path.join(_sk_src, _n, "SKILL.md")) as f:
            _head = f.read(4000)
        if not re.search(r"^name:\s*" + re.escape(_n) + r"\s*$", _head, re.M) \
           or not re.search(r"^description:\s*\S", _head, re.M):
            _bad_front.append(_n)
    check("every shipped skill's frontmatter names ITSELF and carries a description — a skill whose "
          "name disagrees with its directory is loaded under a name nobody can invoke",
          not _bad_front)
    if _bad_front:
        print("       offenders: " + ", ".join(_bad_front))

    _skh = tempfile.mkdtemp(prefix="gameloop-skills-")
    try:
        _sdest = os.path.join(_skh, "skills")

        def _inst(*args, **extra):
            return subprocess.run([os.path.join(REPO, "install.sh"), *args],
                                  capture_output=True, text=True,
                                  env=_env(GAME_LOOP_SKILLS_DIR=_sdest, **extra))

        _r = _inst("--skills-only")
        _landed = sorted(os.listdir(_sdest)) if os.path.isdir(_sdest) else []
        check("--skills-only installs every shipped skill and touches no project",
              _r.returncode == 0 and _landed == _sk_names)
        check("...and each landed entry actually resolves to a readable SKILL.md, so a broken "
              "symlink cannot pass as an installed skill",
              all(os.path.isfile(os.path.join(_sdest, n, "SKILL.md")) for n in _landed))
        # From a CHECKOUT they are symlinks, so `git pull` in that checkout upgrades them all. (The
        # fetched-payload path copies instead — its source is a temp dir this installer deletes on
        # exit, and a link into it would dangle within the second.)
        check("...as symlinks into the source checkout, which is what makes updating them a pull",
              all(os.path.islink(os.path.join(_sdest, n)) for n in _landed))

        _r2 = _inst("--skills-only")
        check("...and re-running is idempotent rather than stacking or failing",
              _r2.returncode == 0 and sorted(os.listdir(_sdest)) == _sk_names)

        # SOMEBODY ELSE'S SKILL, in both shapes it can arrive in: a plain directory somebody wrote by
        # hand, and a symlink another tool installed.
        _victim = os.path.join(_sdest, _sk_names[0])
        os.remove(_victim) if os.path.islink(_victim) else shutil.rmtree(_victim)
        os.makedirs(_victim)
        with open(os.path.join(_victim, "SKILL.md"), "w") as f:
            f.write("---\nname: " + _sk_names[0] + "\ndescription: MINE, hand written\n---\n")
        _other = os.path.join(_skh, "someone-elses", _sk_names[1])
        os.makedirs(_other)
        with open(os.path.join(_other, "SKILL.md"), "w") as f:
            f.write("---\nname: " + _sk_names[1] + "\ndescription: THEIRS\n---\n")
        _link_victim = os.path.join(_sdest, _sk_names[1])
        os.remove(_link_victim) if os.path.islink(_link_victim) else shutil.rmtree(_link_victim)
        os.symlink(_other, _link_victim)

        _r3 = _inst("--skills-only")

        def _read(*p):
            with open(os.path.join(*p)) as f:
                return f.read()

        check("a hand-written skill of the same name is KEPT, not overwritten — the collision is the "
              "only evidence it existed, so replacing it destroys the notice too",
              "MINE, hand written" in _read(_victim, "SKILL.md"))
        check("...and a symlink pointing at somebody ELSE's tree is kept too",
              os.path.islink(_link_victim) and "THEIRS" in _read(_link_victim, "SKILL.md"))
        check("...and both refusals are SAID rather than passed over in silence, naming what was "
              "kept and where ours is",
              _r3.returncode == 0 and _r3.stdout.count("kept") >= 2
              and _sk_names[0] in _r3.stdout and _sk_names[1] in _r3.stdout)
        check("...while every OTHER skill still installs — one foreign name does not stop the rest",
              all(os.path.isfile(os.path.join(_sdest, n, "SKILL.md")) for n in _sk_names))

        # THE CONSENT HALF. A project install must not reach into the user's home uninvited, and a
        # run with nobody to ask is not consent — it is the absence of an answer.
        _sdest2 = os.path.join(_skh, "skills-untouched")
        _proj = os.path.join(_skh, "proj")
        os.makedirs(_proj)
        _rp = subprocess.run([os.path.join(REPO, "install.sh"), _proj],
                             capture_output=True, text=True,
                             env=_env(GAME_LOOP_SKILLS_DIR=_sdest2))
        check("a plain install with no tty to ask at installs NO skills — silence is not a yes for "
              "the one action that writes outside the directory the user named",
              _rp.returncode == 0 and not os.path.exists(_sdest2))
        check("...and it SAYS so, with the command that adds them later, rather than leaving the "
              "user to discover a feature that never appeared",
              "no terminal to ask at" in _rp.stdout and "--skills-only" in _rp.stdout)
        check("...and nothing about skills is written into the TARGET either — they are user-level, "
              "and a copy in the project would be a second one to drift",
              not os.path.exists(os.path.join(_proj, ".claude", "skills")))

        _proj2 = os.path.join(_skh, "proj2")
        os.makedirs(_proj2)
        _rn = subprocess.run([os.path.join(REPO, "install.sh"), "--no-skills", _proj2],
                             capture_output=True, text=True,
                             env=_env(GAME_LOOP_SKILLS_DIR=_sdest2))
        check("--no-skills is honoured and asks nothing",
              _rn.returncode == 0 and not os.path.exists(_sdest2)
              and "Install them?" not in _rn.stdout)

        _rc = subprocess.run([os.path.join(REPO, "install.sh"), "--skills-only", _proj2],
                             capture_output=True, text=True,
                             env=_env(GAME_LOOP_SKILLS_DIR=_sdest2))
        check("--skills-only with a target directory is REFUSED rather than silently ignoring one "
              "of the two things it was told to do",
              _rc.returncode == 1 and "takes no target" in _rc.stderr)
    finally:
        shutil.rmtree(_skh, ignore_errors=True)

    print("the house voice is enforced, not remembered (#33):")
    _w = "sys" + "tem"
    _banned = re.compile(r"\b" + _w + r"s?\b", re.I)
    _MARK = "theme-word-" + "ok"

    def _offending_lines(text):
        """Word-boundary, so SystemExit and filesystem are DIFFERENT words and pass untouched —
        a rule that fired on those would be reworded away rather than obeyed."""
        return [i for i, ln in enumerate(text.split("\n"), 1)
                if _banned.search(ln) and _MARK not in ln]

    check("the scanner catches the word, in both singular and plural, and ignores the compounds "
          "that merely contain it",
          _offending_lines("a " + _w + " here") == [1]
          and _offending_lines("two " + _w + "s") == [1]
          and _offending_lines("raise " + _w.capitalize() + "Exit(2)") == []
          and _offending_lines("the file" + _w + " path") == [])
    check("...and an explicitly marked line is skipped, so an exception is greppable rather than "
          "an argument with the check",
          _offending_lines(f"a {_w} here  # {_MARK}") == [])

    _tracked = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True)
    _files = [f for f in _tracked.stdout.split("\n") if f.strip()]
    if not _files:
        # Same reason as the sweep's denominator: in an EXTRACTED tree git answers nothing, and a
        # scan over an empty file list passes in perfect silence — the rule would read as enforced
        # everywhere precisely where nobody could check it.
        _skip = {".git", "__pycache__", ".worktrees", ".game_loop_self", "node_modules"}
        for _r, _ds, _fs in os.walk(REPO):
            _ds[:] = [d for d in _ds if d not in _skip]
            _files += [os.path.relpath(os.path.join(_r, x), REPO) for x in _fs]
    _viol = []
    for _f in _files:
        try:
            with open(os.path.join(REPO, _f)) as fh:
                _txt = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        _viol += [f"{_f}:{i}" for i in _offending_lines(_txt)]
    check("NO tracked file uses the banned word — the rule now holds over the whole repo, including "
          "the shipped payload and the docs, not just the files someone re-read",
          not _viol)
    if _viol:
        print("       offenders: " + ", ".join(_viol[:8]))
    check("...and the scan actually looked at the repo, so an empty answer is a verdict rather "
          "than a walk that found no files",
          len(_files) > 20 and "CLAUDE.md" in _files)
    check("the rule itself lives in a TRACKED file now, not in gitignored scratch — a rule that "
          "does not reach a fresh clone is an oral tradition",
          _MARK in open(os.path.join(REPO, "CLAUDE.md")).read())

    # #50 — THE SUITE MUST NOT BE ABLE TO HANG on the reply poll. A hang yields no verdict at all
    # and looks, from outside, exactly like a slow machine; it also made notify.replies impossible
    # for the sweep to measure, since the tool's method cannot terminate on it. Structural, because
    # the failure it guards against is a NEW call site added without a bound — which no behavioural
    # assertion would catch until the day something hangs.
    print("the suite cannot hang on the watchdog's reply poll (#50):")
    _own = open(os.path.join(REPO, "test", "run.py")).read()
    _raw = [ln for ln in _own.split("\n")
            if "subprocess.run([wd_bin]" in ln and "def run_watchdog" not in ln]
    check("every watchdog drive goes through the bounded runner — exactly one raw call, inside it",
          len(_raw) == 1)
    check("...and that runner passes a timeout and turns a hang into a NAMED failure, never a "
          "silent 2 that would satisfy an assertion about a ring",
          "timeout=WATCHDOG_TIMEOUT_SEC" in _own and "TimeoutExpired" in _own
          and "returncode = None" in _own)
    check("...while the product loop keeps waiting indefinitely, which is correct there — a human "
          "who has not answered still holds the ball",
          "while True:" in open(os.path.join(SRC_GAME_LOOP, "bin", "watchdog")).read())

    print("producer mutation sweep (test/mutation_sweep.py):")
    _spec = importlib.util.spec_from_file_location(          # the real file, not a copy of it
        "mutation_sweep", os.path.join(REPO, "test", "mutation_sweep.py"))
    sweep = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sweep)
    check("zero kills is UNPROTECTED — the case the sweep exists for, and the only fatal one",
          sweep.verdict(0) == sweep.UNPROTECTED)
    check("one kill is THIN, not unprotected — it noticed, but on a single assertion",
          sweep.verdict(1) == sweep.THIN)
    check(f"the thin line is a boundary, not a slope: {sweep.THIN_AT - 1} thin, "
          f"{sweep.THIN_AT} ok",
          sweep.verdict(sweep.THIN_AT - 1) == sweep.THIN
          and sweep.verdict(sweep.THIN_AT) == sweep.OK)
    check("THIN and UNPROTECTED are distinct verdicts — thin argues, only zero blocks",
          sweep.THIN != sweep.UNPROTECTED and sweep.OK not in (sweep.THIN, sweep.UNPROTECTED))
    # A producer renamed out from under the sweep is the quiet failure it could most easily have:
    # neuter() would not find it, the entry would report nothing, and the count would look fine.
    with open(os.path.join(SRC_GAME_LOOP, "bin", "game_loop")) as f:
        gl_src = f.read()
    # Keys are "<relpath>::<name>" now (#44): the denominator is every source file, not one, and a
    # bare-name namespace could not tell two implementations of one name apart.
    def _mut_src(key):
        with open(os.path.join(REPO, key.split("::")[0])) as f:
            return f.read()

    missing = [m[1] for m in sweep.MUTANTS
               if not sweep.neuter(_mut_src(m[1]), m[1].split("::")[1], "    pass\n")[1]]
    check("every producer the sweep names still exists in the file it names — no silent no-op entry",
          not missing)
    check("...and every entry is FILE-QUALIFIED, so one name in two files cannot collapse into one "
          "decision",
          all("::" in m[1] for m in sweep.MUTANTS)
          and all("::" in k for k in sweep.NOT_SWEPT))
    # Every entry must carry a recorded FLOOR, or its count is prose again: the sweep compares
    # against it and fails on a drop, which is the difference between a number that is checked and
    # one that is merely written down. Indexing rather than unpacking above, so adding a field to
    # MUTANTS cannot break this check — which is exactly how it broke when the floor was added.
    check("every producer carries a recorded floor, so a drop in coverage is caught not narrated",
          all(len(m) >= 6 and isinstance(m[5], int) and m[5] >= 0 for m in sweep.MUTANTS))
    # CONCURRENCY MUST NOT COST THE REPORT (#59). Producers are independent by construction, so
    # running them at once is free — but only if two things hold: no producer prints as it goes
    # (concurrent writes interleave into an unreadable report), and the emitted order is the order
    # of MUTANTS rather than of who happened to finish first. A sweep whose output shuffles run to
    # run cannot be diffed against the previous one, which is most of what a floor is for.
    # sweep_one is nested inside main(), so it cannot be reached with getattr — pull it out of the
    # tree instead. Asserting on the whole of main() would be useless here: main() prints plenty.
    _sweep_src = open(os.path.join(REPO, "test", "mutation_sweep.py")).read()
    _one_src = ""
    for _n in ast.walk(ast.parse(_sweep_src)):
        if isinstance(_n, ast.FunctionDef) and _n.name == "sweep_one":
            _one_src = ast.get_source_segment(_sweep_src, _n) or ""
    check("the per-producer worker RETURNS its report instead of printing it — 6 producers printing "
          "as they go would interleave into one unreadable report",
          bool(_one_src) and "print(" not in _one_src and "return" in _one_src)

    # BEHAVIOURAL: drive the real main() with the suite stubbed, and make completion order the
    # REVERSE of MUTANTS order. If the emit logic followed completion, this is what would catch it.
    _fake = [("alpha -> x", ".game_loop/bin/game_loop::unpushed_warning", "    return None\n",
              ["unpushed"], None, 0),
             ("beta -> x", ".game_loop/bin/game_loop::aggregate_tell", "    return None\n",
              ["aggregate"], None, 0),
             ("gamma -> x", ".game_loop/bin/game_loop::ruled_out", "    return None\n",
              ["ruled"], None, 0)]
    _delays = {0: 0.45, 1: 0.25, 2: 0.05}     # first submitted finishes LAST
    _calls = []

    def _stub_run(tree):
        # The baseline call arrives before any mutant; after that, one call per producer.
        n = len(_calls)
        _calls.append(tree)
        if n:
            time.sleep(_delays.get(n - 1, 0))
        return "  ok   assertion one\n  ok   assertion two\n1 passed, 0 failed\n"

    _real_run, _real_mut, _real_gate = sweep.run, sweep.MUTANTS, sweep.coverage_gate
    _buf = io.StringIO()
    try:
        sweep.run, sweep.MUTANTS = _stub_run, _fake
        sweep.coverage_gate = lambda found: False      # accounting is asserted above, not here
        with contextlib.redirect_stdout(_buf):
            sweep.main()
    finally:
        sweep.run, sweep.MUTANTS, sweep.coverage_gate = _real_run, _real_mut, _real_gate
    _out = _buf.getvalue()
    _order = [_out.find(lbl) for lbl in ("alpha -> x", "beta -> x", "gamma -> x")]
    check("every producer is reported, and in MUTANTS order even though they finished in exactly "
          "the reverse — the report is diffable against the last run, not a race",
          all(p >= 0 for p in _order) and _order == sorted(_order))
    check("...and the run reports what it COST, so the next argument about this check's runtime "
          "starts from a number instead of an impression",
          "producer time:" in _out and " at a time" in _out)
    check("...and it says how many ran at once, since a wall-clock figure means nothing without it",
          "running 3 producers," in _out)

    # neuter() is the whole mechanism: if it returned the source unchanged while reporting success,
    # every producer would come back perfectly protected and nothing would have been mutated.
    mutated, found = sweep.neuter(gl_src, "aggregate_tell", "    return None\n")
    check("neuter actually replaces the body it claims to — the sweep's one load-bearing edit",
          found and "def aggregate_tell(text):\n    return None\n" in mutated
          and "_AGGREGATE_TELLS" in mutated and mutated != gl_src)

    # .game_loop/config.json is TRACKED — correct, it is project config — and two of its fields take
    # filesystem paths. A downstream project filled allow_write_roots with an absolute path under the
    # author's home and pushed to a public repo: every cloner inherited a WRITE ROOT outside their
    # repo, aimed at a directory only the author had. `expanduser` is already applied to these entries
    # (bin/guard-writes-impl.sh), so the tilde form was portable and correct all along and nothing
    # anywhere said so. status now says it. Every silence below is a DIFFERENTIAL against a run of the
    # same check, in the same sandbox, that was seen to SPEAK — a check that has stopped working is
    # quiet for the tilde form, quiet for an untracked config and quiet for everything else.
    print("config paths (a tracked config granting a write root on ONE machine):")
    cp = make_sandbox()
    cpcfg = os.path.join(cp, ".game_loop", "config.json")
    cphome = os.path.expanduser("~")
    cpabs = os.path.join(cphome, "gl-cfgpath-scratch")

    def cpgit(*args):
        return subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=tester",
                               "-c", "commit.gpgsign=false", *args],
                              cwd=cp, capture_output=True, text=True)

    def cpset(**fields):
        base = {"project_name": "t", "update_check": False}   # never reach the network from here
        base.update(fields)
        with open(cpcfg, "w") as f:
            json.dump(base, f)

    def cpstatus(env=None):
        return subprocess.run([os.path.join(cp, ".game_loop", "bin", "game_loop"), "status"],
                              capture_output=True, text=True, env=env or _env())

    cpgit("init", "-q")
    cpset(allow_write_roots=[cpabs])
    cpgit("add", ".game_loop/config.json")
    warned = cpstatus().stdout
    check("a TRACKED config with a home-absolute allow_write_roots warns, and names the entry",
          "CONFIG PATHS" in warned and f"⚠ allow_write_roots: {cpabs}" in warned)
    check("...and says what it MEANS: committed, so every cloner inherits a write root they never "
          "chose",
          "EVERY clone of this repo inherits it" in warned
          and "allowlisted WRITE" in warned and "INV3" in warned)
    check("...and names the remedy that already works — the tilde form, resolved by expanduser",
          "→ write it as  ~/gl-cfgpath-scratch" in warned and "expanduser" in warned
          and "guard-writes-impl.sh" in warned)
    check("...and rules OUT the plausible wrong fix: a relative entry resolves against the cwd",
          "a RELATIVE entry is NOT the fix" in warned)
    check("...and states its own reach rather than implying it is complete (INV6)",
          "NOT checked:" in warned and "published" in warned)

    cpset(allow_write_roots=["~/gl-cfgpath-scratch"])
    tilde = cpstatus().stdout
    check("the TILDE form of the same path does not warn — and the absolute one did, so that "
          "silence is a verdict",
          "CONFIG PATHS" not in tilde and "CONFIG PATHS" in warned)

    cpset(allow_write_roots=[], read_roots=[])
    empty = cpstatus().stdout
    check("empty arrays do not warn — the shipped default is not a finding",
          "CONFIG PATHS" not in empty and "CONFIG PATHS" in warned)

    cpset(read_roots=[os.path.join(cphome, "gl-cfgpath-notes")])
    readonly = cpstatus().stdout
    check("a home-absolute read_roots is reported MILDLY — same shape, no permission in it",
          "· read_roots: " in readonly and "no permission in it" in readonly
          and "⚠ allow_write_roots" not in readonly)
    check("...and its remedy cites the reader that actually expands it, not the write guard",
          "claim --read, .game_loop/bin/game_loop" in readonly
          and "guard-writes-impl.sh" not in readonly and "guard-writes-impl.sh" in warned)

    other = os.path.join(os.path.dirname(cphome), "gl-cfgpath-nobody", "scratch")
    cpset(allow_write_roots=[other])
    foreign = cpstatus().stdout
    check("an entry under ANOTHER account's home warns too — that is the cloner's view of this bug",
          'home of "gl-cfgpath-nobody"' in foreign and "NOT this account" in foreign
          and f"⚠ allow_write_roots: {other}" in foreign)

    # The whole point of gating on TRACKED: a project that gitignores .game_loop/ has no exposure,
    # and a warning that fires where there is no hazard is the one that gets tuned out (INV5 from
    # the other side). Same file, same contents, only the tracking changes.
    cpset(allow_write_roots=[cpabs])
    still = cpstatus().stdout
    cpgit("rm", "--cached", "-q", ".game_loop/config.json")
    untracked = cpstatus().stdout
    check("an UNTRACKED config does not warn regardless of contents — nothing is published",
          "CONFIG PATHS" not in untracked and "CONFIG PATHS" in still
          and f"⚠ allow_write_roots: {cpabs}" in still)

    # A git that fails must degrade to silence, never a traceback: this is status output.
    cpgit("add", ".game_loop/config.json")
    fakebin = tempfile.mkdtemp(prefix="gameloop-nogit-")
    with open(os.path.join(fakebin, "git"), "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(os.path.join(fakebin, "git"), 0o755)
    nogit_env = _env()
    nogit_env["PATH"] = fakebin + os.pathsep + nogit_env["PATH"]
    broke = cpstatus(env=nogit_env)
    speaks = cpstatus().stdout
    check("a failing git degrades to silence — no warning, no traceback, and status still runs",
          "CONFIG PATHS" not in broke.stdout and "Traceback" not in broke.stderr
          and broke.returncode == 0 and "=== game_loop" in broke.stdout)
    check("...and the same config on a working git DOES warn, so that silence was git, not a dead "
          "check",
          "CONFIG PATHS" in speaks and f"⚠ allow_write_roots: {cpabs}" in speaks)

    # The moment someone is about to fill these fields in is the cheapest place to say it.
    with open(os.path.join(REPO, "install.sh")) as f:
        inst_src = f.read()
    check("install.sh's next steps teach the tilde convention where config.json is introduced",
          "allow_write_roots" in inst_src and "as ~/..." in inst_src
          and "COMMITTED" in inst_src)

    # The sweep above was a DENYLIST — six hand-written entries, and its coverage was whatever
    # somebody remembered to type. That is the shape bin/guard-writes-impl.sh's header and #25 both
    # argue against: a denylist defaults to UNPROTECTED and misses whatever nobody listed, and the
    # tool built to find unprotected things was unprotected in exactly that way. It now enumerates
    # its own candidates out of the script and refuses to run while any of them is undecided. What
    # is checked here is the ENUMERATION and the REFUSAL, because those are what can rot; the sweep
    # itself is still far too slow to run from this suite.
    print("the sweep cannot strand a mutation in the working tree:")
    # OBSERVED, in a peer project rather than here, and reported by its owner: their sweep mutates
    # the tree IN PLACE and restores in a `finally`. They SIGKILLed a run they thought was stuck,
    # the finally never ran, and FOUR neutered functions sat in their working tree for hours —
    # including one that made a CLI raise NameError on every call. Agents pointing at that checkout
    # ran deliberately broken code the whole time, and the owner spent the day diagnosing around
    # their own tree. I killed my sweep twice today for the same reason, so this is not a hazard I
    # am merely sympathetic to.
    #
    # My sweep happens to be immune: it writes only inside a mkdtemp copy of a `git archive HEAD`
    # extract, so there is no restore step for a missed finally to skip. HAPPENS TO BE is the
    # problem — nothing checked it, so the property was one careless `open(os.path.join(REPO, ...))`
    # away from gone, and its absence would have been silent. A `finally` is not cleanup, because
    # SIGKILL, a sleeping laptop and a power cut all skip it; the only durable answer is for the
    # original never to be the subject.
    _ms_src = open(os.path.join(REPO, "test", "mutation_sweep.py")).read()

    def _writes_outside_tmp(src):
        """Any `open(<path>, "w")` whose path is not rooted in a mkdtemp-derived variable.

        LINE-SCOPED ON PURPOSE. The first version matched across newlines, so a READ —
        `open(os.path.join(tree, rel)) as f:` — paired with a `"w"` several lines later and was
        reported as a write. A scan that reaches past the end of the call it is reading invents
        findings; a write is on one line here, so the check stays on one line too.
        """
        tmp_vars = set(re.findall(r"^\s*(\w+)\s*=\s*tempfile\.mkdtemp", src, re.M))
        bad = []
        for line in src.split("\n"):
            m = re.search(r'open\(\s*([^)]*\)?[^,]*),\s*"w"', line)
            if not m:
                continue
            root = re.match(r"os\.path\.join\(\s*(\w+)", m.group(1))
            if not (root and root.group(1) in tmp_vars):
                bad.append(line.strip()[:70])
        return bad

    check("the sweep writes ONLY inside a temp directory it made — the original is never the "
          "subject, so a kill has nothing to restore and cannot leave the repo neutered",
          not _writes_outside_tmp(_ms_src))
    check("...and the same check CATCHES a write aimed at the repo, so the clean result above is a "
          "verdict rather than a regex that matches nothing",
          _writes_outside_tmp('t = tempfile.mkdtemp()\nopen(os.path.join(REPO, rel), "w")')
          and not _writes_outside_tmp('t = tempfile.mkdtemp()\nopen(os.path.join(t, rel), "w")'))
    check("...and a READ is not mistaken for a write just because a write appears further down — "
          "a scan that reaches past the call it is reading invents findings",
          not _writes_outside_tmp('t = tempfile.mkdtemp()\n'
                                  'with open(os.path.join(tree, rel)) as f:\n'
                                  '    pass\n'
                                  'open(os.path.join(t, rel), "w")'))
    check("...and there IS a write to find — a sweep that mutated nothing would pass the check "
          "above for the wrong reason entirely",
          'open(os.path.join(t, rel), "w")' in _ms_src)
    check("...and its subject is a committed tree, not the working copy: a floor measured against "
          "uncommitted work is a number about a tree nobody else can check out",
          "archive HEAD" in _ms_src and "mkdtemp" in _ms_src)

    # And the mutating function itself is PURE — it returns the new source, it does not write one.
    _wd_path = os.path.join(SRC_GAME_LOOP, "bin", "watchdog")
    _wd_before = open(_wd_path).read()
    _neutered, _found = sweep.neuter(_wd_before, "superseded", "    return False\n")
    check("neuter() hands back a mutated STRING and writes nothing — the caller chooses where it "
          "lands, and here that is only ever a copy",
          _found and "return False" in _neutered and open(_wd_path).read() == _wd_before)

    print("mutation sweep coverage (default-deny over the producers it can find):")
    synth = ("def decides(x):\n"
             "    if x:\n"
             "        return 'a finding'\n"
             "    return None\n"
             "def always(x):\n"
             "    return 'a finding'\n"
             "def never(x):\n"
             "    return []\n"
             "def empties(x):\n"
             "    if x:\n"
             "        return [x]\n"
             "    return []\n")
    check("discovery names the functions that can report OR decline, and neither of the ones that "
          "cannot — a silence is only a shape when a non-silence sits beside it",
          sweep.candidates(synth) == ["decides", "empties"])
    real_cands = sweep.candidates(gl_src)
    check("...and on the real script it finds the known producers, including the ones the "
          "hand-written list had never been pointed at — while a report that cannot decline is not "
          "swept up with them",
          {"unpushed_warning", "hooks_live_warning", "config_paths_report", "worktree_report",
           "update_notice"} <= set(real_cands)
          and "pins_report" not in real_cands)

    # MONOTONICITY. Widening what counts as a "nothing" made the candidate set SHRINK — `any slot
    # is nothing` marked `(value, None)` as a nothing, which cost such functions their OTHER arm, and
    # `candidates()` needs a PAIR. `_home_keyed` was enumerated before that change and not after: the
    # denominator bug this file exists to catch, committed inside a fix for it. Caught by the stale
    # exclusion gate, not by intent, so it gets an assertion of its own.
    _narrow = sweep._returns_nothing

    def _old_rule(ret):
        """The definition BEFORE tuples were understood — bare, None, False, []."""
        v = ret.value
        if v is None:
            return True
        if isinstance(v, ast.Constant) and (v.value is None or v.value is False):
            return True
        return isinstance(v, ast.List) and not v.elts

    try:
        sweep._returns_nothing = _old_rule
        _before = set(sweep.all_candidates())
    finally:
        sweep._returns_nothing = _narrow
    _after = set(sweep.all_candidates())
    check("a wider definition of 'reports nothing' may only ADD producers — the version that "
          "dropped one while claiming to widen is the short-denominator bug inside its own fix",
          _before <= _after and len(_after) > len(_before))
    check("...and the assertion can actually fail: the rejected 'any slot' rule loses a producer "
          "the narrow rule found, so the subset above is a verdict rather than a tautology",
          any(k not in {
              f"{rel}::{n}"
              for rel in sweep.source_files(sweep.REPO)
              for n in (lambda src: [fn.name for fn in
                                     [x for x in ast.walk(ast.parse(src))
                                      if isinstance(x, ast.FunctionDef)]
                                     if any((lambda r: r.value is not None
                                             and isinstance(r.value, ast.Tuple)
                                             and any(_old_rule(ast.Return(value=e))
                                                     for e in r.value.elts))(r)
                                            or _old_rule(r)
                                            for r in ast.walk(fn) if isinstance(r, ast.Return))
                                     and any(not ((lambda r: r.value is not None
                                                   and isinstance(r.value, ast.Tuple)
                                                   and any(_old_rule(ast.Return(value=e))
                                                           for e in r.value.elts))(r)
                                                  or _old_rule(r))
                                             for r in ast.walk(fn) if isinstance(r, ast.Return))])(
                  open(os.path.join(sweep.REPO, rel)).read())}
              for k in _before))

    # The gate is the whole point of the change: a producer nobody decided about has to FAIL the
    # run, not be quietly absent from it. Checked in BOTH directions in one observation, because "the
    # gate passed" and "the gate is dead" produce the same output.
    said = []
    real_found = sweep.all_candidates()
    orphaned = dict(real_found)
    orphaned[".game_loop/bin/game_loop::gl_test_undecided_producer"] = (
        ".game_loop/bin/game_loop", "gl_test_undecided_producer")
    rc_bad = sweep.coverage_gate(orphaned, out=said.append)
    rc_good = sweep.coverage_gate(real_found, out=said.append)
    check("a candidate in neither MUTANTS nor NOT_SWEPT fails the sweep and is NAMED in the "
          "refusal — while the real script, fully decided, passes the same gate",
          rc_bad == 1 and rc_good == 0
          and any("UNACCOUNTED PRODUCERS" in ln for ln in said)
          and any("gl_test_undecided_producer" in ln for ln in said))
    check("...and main() actually runs that gate, rather than defining it next to a sweep that "
          "never calls it",
          "coverage_gate(" in inspect.getsource(sweep.main))

    # An exclusion with no reason is a name on a list: unreadable, uncheckable, and exactly what
    # somebody clearing the run to get a green would leave behind.
    check("every exclusion carries a reason — and the same check catches a blank one, so its "
          "silence on the real mapping is a verdict rather than a dead comprehension",
          sweep.unreasoned() == []
          and sweep.unreasoned({"blank": "", "spaces": "   ", "given": "a real reason"})
          == ["blank", "spaces"])
    check("no producer is decided about twice — and the same check names one when it is, so the "
          "empty answer above is not a check that cannot fire",
          sweep.decided_twice() == []
          and sweep.decided_twice(mutants=[("l", "unpushed_warning", "b", [], None, 1)],
                                  not_swept={"unpushed_warning": "r"}) == ["unpushed_warning"])
    # A renamed producer whose exclusion outlives it is the denylist bug coming back by the side
    # door: the name stays decided forever while the function it excused is gone. Driven through the
    # gate against a script those names have LEFT, so the clean answer above is a verdict.
    stale_said = []
    rc_stale = sweep.coverage_gate({}, out=stale_said.append)     # a tree those names have LEFT
    check("every excluded name is a producer the repo still HAS — and the same gate refuses a "
          "tree they have left, naming the stale exclusion",
          set(sweep.NOT_SWEPT) <= set(real_found) and rc_good == 0
          and rc_stale == 1 and any("STALE EXCLUSION" in ln for ln in stale_said))

    # #44 — THE DENOMINATOR. Every fix in this chain made the accounting read COMPLETE while the gap
    # moved one level further out; this link was the FILE LIST, which was one file of five. The set
    # now comes from git rather than from anyone's memory.
    _srcs = sweep.source_files()
    check("the source set is asked of git and spans the whole repo, not one file",
          len(_srcs) > 1 and ".game_loop/bin/game_loop" in _srcs)
    # AND IT DOES NOT COLLAPSE WHERE GIT CANNOT ANSWER. An EXTRACTED tree — the shape a packager
    # gates, with no .git — used to fall back to the single file, so the accounting reported
    # "0 unaccounted" over a set of one, silently, exactly where it mattered most. That is #44
    # reintroduced by environment. Driven against a real extraction rather than reasoned about.
    _ex = tempfile.mkdtemp(prefix="gl-extract-")
    try:
        shutil.copytree(os.path.join(REPO, ".game_loop"), os.path.join(_ex, ".game_loop"),
                        ignore=shutil.ignore_patterns("sessions", "probe", "*.pid", "log.jsonl",
                                                      "triggers.d", "triggers.json"))
        shutil.copytree(os.path.join(REPO, "test"), os.path.join(_ex, "test"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        check("...and a tree with NO .git enumerates the same source set by walking, so the "
              "denominator cannot shrink to one file wherever git happens to be absent",
              not os.path.exists(os.path.join(_ex, ".git"))
              and sorted(sweep.source_files(_ex)) == sorted(
                  f for f in _srcs if os.path.exists(os.path.join(_ex, f))))
    finally:
        shutil.rmtree(_ex, ignore_errors=True)
    check("...including the EXTENSIONLESS scripts, which a *.py glob would have missed entirely — "
          "and they hold the producers that matter most",
          {".game_loop/bin/verify", ".game_loop/bin/watchdog"} <= set(_srcs))
    check("...while config that merely PARSES as Python is excluded: ast.parse accepts JSON and "
          "YAML, so 'it parses' over-includes and a list that is mostly noise gets skimmed",
          not any(f.endswith((".json", ".yaml", ".yml")) for f in _srcs))
    check("...and the producers this issue named are now IN the denominator rather than absent",
          {".game_loop/bin/verify::owed",
           ".game_loop/bin/watchdog::exhausted_windows"} <= set(real_found))
    # The regression this closes: a NEW source file arriving with an undecided producer must fail
    # the run rather than be quietly outside the count.
    _newfile = dict(real_found)
    _newfile["tools/newly_added.py::reports_or_declines"] = ("tools/newly_added.py",
                                                             "reports_or_declines")
    _nf_said = []
    check("a producer in a file nobody has swept yet fails the gate and is NAMED, so a new script "
          "cannot arrive silently outside the denominator",
          sweep.coverage_gate(_newfile, out=_nf_said.append) == 1
          and any("tools/newly_added.py::reports_or_declines" in ln for ln in _nf_said))

    # The owning agent runs the harness it would SHIP, not the one it is editing: every game_loop
    # hook prefers a pinned checkout when one exists and falls back to the repo's own bin/ when it
    # does not. The dispatch is INLINE in the hook command on purpose — a separate shim file would
    # be one more script sitting in the edit zone, which is the exposure this exists to remove.
    #
    # These assert the SELECTION, not the verdict. Both arms deny an out-of-repo write, so "it
    # denied" cannot tell which copy ran — the same discrimination failure this suite spent #41 on.
    print("pinned dispatch (the agent runs what it would ship):")
    with open(os.path.join(REPO, ".claude", "settings.json")) as f:
        _hooks = json.load(f)["hooks"]
    _cmds = [h["command"] for v in _hooks.values() for e in v for h in e.get("hooks", [])
             if ".game_loop" in h.get("command", "")]
    check("every game_loop hook dispatches through the pin check, not straight at bin/",
          _cmds and all(".game_loop_self" in c and "GAME_LOOP_HOME=" in c for c in _cmds))
    # The guidance `self` PRINTS must be the wiring this repo actually ships. Two hand-maintained
    # copies of one command line drift, and the copy that drifts is the one nobody runs — so the
    # generated text is asserted equal to the tracked settings, not merely similar to it.
    _gen = sorted(l.strip() for l in gl(REPO, "self").stdout.splitlines()
                  if l.strip().startswith('d="$CLAUDE_PROJECT_DIR/'))
    # Compared as SETS: one command legitimately serves two events (SessionStart and PostCompact
    # run the same verb), so a list comparison would fail on the duplicate rather than on drift.
    check("the wiring `self` prints is byte-identical to the wiring settings.json carries",
          _gen and sorted(set(_gen)) == sorted(set(_cmds)))
    check("...and each still names the script it runs, so the wiring stays readable",
          all(any(t in c for t in ("guard-writes.sh", "guard-mcp.sh", "game_loop limitgate",
                                   "game_loop stopgate", "game_loop sessionstart",
                                   "watchdog")) for c in _cmds))

    def _select(root, pinned):
        """Run only the dispatcher's path-resolution half and report which tree it chose."""
        sel = ('d="$CLAUDE_PROJECT_DIR/.game_loop_self/.game_loop"; '
               '[ -x "$d/bin/game_loop" ] || d="$CLAUDE_PROJECT_DIR/.game_loop"; echo "$d"')
        return subprocess.run(["bash", "-c", sel], capture_output=True, text=True,
                              env=_env(root)).stdout.strip()

    _disp = make_sandbox()
    try:
        _pin = os.path.join(_disp, ".game_loop_self", ".game_loop", "bin")
        os.makedirs(_pin)
        with open(os.path.join(_pin, "game_loop"), "w") as f:
            f.write("#!/usr/bin/env python3\n")
        os.chmod(os.path.join(_pin, "game_loop"), 0o755)
        _with = _select(_disp, True)
        check("a pinned checkout present → the hook runs the PIN, not the repo copy",
              _with.endswith(os.path.join(".game_loop_self", ".game_loop")))
        shutil.rmtree(os.path.join(_disp, ".game_loop_self"))
        _without = _select(_disp, False)
        check("...and with no pin it falls back to the repo — a fresh clone is still guarded",
              _without.endswith(".game_loop") and ".game_loop_self" not in _without
              and _with != _without)
    finally:
        shutil.rmtree(_disp, ignore_errors=True)

    # ---- #76: the upstream watcher. Five states, and the two that get merged are the point. ----
    _up = tempfile.mkdtemp(prefix="gl_upstream_")
    try:
        _uh = os.path.join(_up, ".game_loop")
        shutil.copytree(os.path.join(REPO, ".game_loop"), _uh,
                        ignore=shutil.ignore_patterns("sessions", "log.jsonl", "state.json",
                                                      "upstream.json", "config.local.json"))
        _uld = importlib.machinery.SourceFileLoader(
            "gl_up", os.path.join(_uh, "bin", "game_loop"))
        os.environ["GAME_LOOP_HOME"] = _uh
        _u = importlib.util.module_from_spec(importlib.util.spec_from_loader("gl_up", _uld))
        _uld.exec_module(_u)
        os.environ.pop("GAME_LOOP_HOME", None)

        _feed = {}

        def _stub(repo, timeout=25):
            v = _feed.get(repo)
            return v if v else (None, None, "stubbed outage")

        _u._upstream_fetch = _stub

        def _cfg(repos):
            with open(os.path.join(_uh, "config.json")) as f:
                d = json.load(f)
            d["upstream_repos"] = repos
            with open(os.path.join(_uh, "config.json"), "w") as f:
                json.dump(d, f)

        def _wipe():
            try:
                os.remove(_u.UPSTREAM_F)
            except OSError:
                pass

        # INERT BY DEFAULT. An unscoped watcher surfaced unrelated side projects on the reporter's
        # first attempt and "would have been switched off inside a day" — so empty means silent,
        # not means-everything.
        _cfg([])
        _wipe()
        _l, _st = _u.upstream_check()
        check("#76: no upstream_repos configured → the watcher is INERT, not implicitly global",
              _st == "off" and _l == [])

        # FIRST RUN records the world and reports NO items.
        _cfg(["o/a"])
        _feed["o/a"] = ({"1": {"updatedAt": "T1", "title": "one"},
                         "2": {"updatedAt": "T1", "title": "two"}}, "v1", None)
        _l, _st = _u.upstream_check()
        _txt = "\n".join(_l)
        check("#76: the FIRST run records a baseline and reports no items — a gate whose first act "
              "is a backlog is a gate somebody removes",
              _st == "first" and "baseline recorded" in _txt
              and "#1" not in _txt and "#2" not in _txt)

        # QUIET is not a clean bill, and says so.
        _l, _st = _u.upstream_check()
        check("#76: nothing moved → QUIET, and it is named weak evidence about the INDEX rather "
              "than a clean bill",
              _st == "quiet" and "WEAK EVIDENCE" in "\n".join(_l)
              and "lags publication" in "\n".join(_l))

        # MOVEMENT: a reply, a new issue, a close-shaped absence, a release.
        _feed["o/a"] = ({"1": {"updatedAt": "T2", "title": "one"},
                         "3": {"updatedAt": "T2", "title": "three"}}, "v2", None)
        _l, _st = _u.upstream_check()
        _txt = "\n".join(_l)
        check("#76: an issue whose updatedAt moved is reported",
              _st == "movement" and "o/a#1" in _txt and "moved" in _txt)
        check("#76: ...an issue absent from a state:open search is reported as NO LONGER OPEN — "
              "worded as the inference it is, not asserted as a close",
              "o/a#2" in _txt and "no longer open" in _txt)
        check("#76: ...a new issue involving you is reported", "o/a#3" in _txt and "NEW" in _txt)
        check("#76: ...and a RELEASE is watched, because 'did the fix I need ship' is a release or "
              "a close and never a commit",
              "RELEASE v1 -> v2" in _txt)
        check("#76: the movement caveat says it has read NOTHING — a label change and the reply "
              "you are blocked on are the same event here",
              "MOVEMENT IS NOT AN ANSWER" in _txt)

        # THE CAVEAT IS UNCONDITIONAL. One that appears only on ambiguous runs teaches that its
        # absence means certainty, which is the false green this repo exists to refuse.
        _l2, _ = _u.upstream_check()
        check("#76: a caveat is printed on EVERY run, quiet or not — an intermittent caveat teaches "
              "that its absence means certainty",
              any("WEAK EVIDENCE" in x or "NOT AN ANSWER" in x for x in _l2))

        # COULD NOT LOOK is not QUIET. This is the whole three-outcome contract.
        _feed.pop("o/a")
        _before = open(_u.UPSTREAM_F).read()
        _l, _st = _u.upstream_check()
        _txt = "\n".join(_l)
        check("#76: every repo failing is COULD NOT CHECK, a distinct state from 'no movement'",
              _st == "outage" and "COULD NOT CHECK" in _txt
              and "upstream: no movement" not in _txt   # the REPORT line; the caveat says the
              and "WEAK EVIDENCE" not in _txt)          # phrase on purpose, to deny it
        check("#76: ...it says the baseline is UNCHANGED and the next run will report what moved "
              "in the meantime — outage, not hole",
              "NOT 'no movement'" in _txt and "UNCHANGED" in _txt)
        check("#76: ...and the stored baseline is in fact untouched by a failed check",
              open(_u.UPSTREAM_F).read() == _before)

        # PARTIAL, and the property that makes an outage temporary instead of permanent.
        _cfg(["o/a", "o/b"])
        _feed["o/a"] = ({"1": {"updatedAt": "T9", "title": "one"}}, "v2", None)
        _wipe()
        _feed["o/b"] = ({"7": {"updatedAt": "B1", "title": "b-one"}}, "v1", None)
        _u.upstream_check()                     # baseline both
        _b_before = json.load(open(_u.UPSTREAM_F))["repos"]["o/b"]
        _feed.pop("o/b")                        # now o/b cannot be reached
        _feed["o/a"] = ({"1": {"updatedAt": "TZ", "title": "one"}}, "v2", None)
        _l, _st = _u.upstream_check()
        _txt = "\n".join(_l)
        check("#76: one repo up and one down is PARTIAL — reported as a subset, not as a result",
              _st == "partial" and "COULD NOT BE CHECKED" in _txt
              and "A PARTIAL RESULT IS NOT A RESULT" in _txt)
        _b_after = json.load(open(_u.UPSTREAM_F))["repos"]["o/b"]
        check("#76: THE LOAD-BEARING ONE — an unchecked repo's baseline does NOT advance, so one "
              "outage cannot become a permanent blind spot the next run reports as calm",
              _b_after == _b_before)
        check("#76: ...while the repo that DID answer still advances, so a partial run is not a "
              "wasted one",
              json.load(open(_u.UPSTREAM_F))["repos"]["o/a"]["issues"]["1"]["updatedAt"] == "TZ")

        # The wording rules, checked as text because that is what they are.
        for _name, _c in (("movement", _u._UP_MOVEMENT_CAVEAT), ("quiet", _u._UP_QUIET_CAVEAT),
                          ("partial", _u._UP_PARTIAL_CAVEAT), ("outage", _u._UP_OUTAGE_CAVEAT)):
            check(f"#76: the {_name} caveat is a FIXED string — no interpolation, because an "
                  "assembled caveat can render empty exactly when it matters most",
                  "{" not in _c and "%s" not in _c and len(_c) > 60)
    finally:
        os.environ.pop("GAME_LOOP_HOME", None)
        shutil.rmtree(_up, ignore_errors=True)

    # ---- the `proved` moment: documentation belongs at the verb that ships, not at the publish ----
    _pv = tempfile.mkdtemp(prefix="gl_proved_")
    try:
        _ph = os.path.join(_pv, ".game_loop")
        shutil.copytree(os.path.join(REPO, ".game_loop"), _ph,
                        ignore=shutil.ignore_patterns("sessions", "log.jsonl", "state.json",
                                                      "upstream.json", "config.local.json",
                                                      "triggers.json", "triggers.d"))
        subprocess.run(["git", "init", "-q", _pv], check=True)
        _tgt = os.path.join(_pv, "subject.py")
        with open(_tgt, "w") as f:
            f.write("VALUE = 1\n")
        _probe = os.path.join(_pv, "probe.py")
        with open(_probe, "w") as f:
            f.write("import sys\nsys.path.insert(0, %r)\n"
                    "import subject\nsys.exit(0 if subject.VALUE == 1 else 1)\n" % _pv)

        def _gl(*args):
            return subprocess.run(
                [os.path.join(_ph, "bin", "game_loop"), *args],
                capture_output=True, text=True, cwd=_pv,
                env=dict(os.environ, GAME_LOOP_HOME=_ph))

        def _wire(evt):
            with open(os.path.join(_ph, "triggers.json"), "w") as f:
                json.dump({evt: [{"name": "docs", "command": "cat >/dev/null; echo DOCS-CHECKED"}]},
                          f)

        _wire("proved")
        _r = _gl("mutate", "--prove", "the probe pins VALUE",
                 "--test", f"python3 {_probe}", "--file", _tgt,
                 "--replace", "VALUE = 1", "--with", "VALUE = 2")
        check("a proof verb fires the `proved` moment, so a documentation check runs when the "
              "change LANDS rather than at publish, a dozen changes later",
              _r.returncode == 0 and "DOCS-CHECKED" in _r.stdout
              and "triggers · proved" in _r.stdout)
        check("...and the file it mutated is restored, so the moment does not fire over a tree "
              "left broken",
              open(_tgt).read() == "VALUE = 1\n")

        # THE CONTROL. "The trigger fired" and "the verb prints that string anyway" produce the
        # same stdout, so the same command with nothing wired has to come back silent.
        with open(os.path.join(_ph, "triggers.json"), "w") as f:
            json.dump({}, f)
        _r2 = _gl("mutate", "--prove", "the probe pins VALUE",
                  "--test", f"python3 {_probe}", "--file", _tgt,
                  "--replace", "VALUE = 1", "--with", "VALUE = 2")
        check("...and with nothing wired the same verb says nothing about triggers — so the line "
              "above is an attachment firing, not the verb's own output",
              _r2.returncode == 0 and "DOCS-CHECKED" not in _r2.stdout
              and "triggers · proved" not in _r2.stdout)

        # A FAILED PROOF MUST NOT FIRE IT. `proved` means "this now behaves differently and it was
        # demonstrated"; firing on a mutation the test slept through would attach documentation
        # work to the exact case where nothing was established.
        _wire("proved")
        _r3 = _gl("mutate", "--prove", "a claim the test does not pin",
                  "--test", "python3 -c \"import sys; sys.exit(0)\"", "--file", _tgt,
                  "--replace", "VALUE = 1", "--with", "VALUE = 3")
        check("a proof that FAILS does not fire `proved` — the moment means something was "
              "demonstrated, and NOT PROVED is the case where nothing was",
              _r3.returncode != 0 and "DOCS-CHECKED" not in _r3.stdout)
    finally:
        shutil.rmtree(_pv, ignore_errors=True)

    # ---- the three the sweep called UNPROTECTED: neutering them killed nothing ----
    _up3 = tempfile.mkdtemp(prefix="gl_unprot_")
    try:
        _u3h = os.path.join(_up3, ".game_loop")
        shutil.copytree(os.path.join(REPO, ".game_loop"), _u3h,
                        ignore=shutil.ignore_patterns("sessions", "log.jsonl", "state.json",
                                                      "upstream.json", "config.local.json",
                                                      "triggers.json", "triggers.d"))
        _l3 = importlib.machinery.SourceFileLoader("gl_u3", os.path.join(_u3h, "bin", "game_loop"))
        os.environ["GAME_LOOP_HOME"] = _u3h
        _g3 = importlib.util.module_from_spec(importlib.util.spec_from_loader("gl_u3", _l3))
        _l3.exec_module(_g3)
        os.environ.pop("GAME_LOOP_HOME", None)

        # pin_status — "is this tree running PINNED code, and at which sha". Neutered to a constant
        # "not pinned" it went in green, which means the harness could have been silently running
        # the repo copy while reporting a pin.
        check("pin_status on a tree with no pinned checkout reports NOT pinned, and names no sha",
              _g3.pin_status(_u3h) == (False, None))
        _pind = os.path.join(_up3, _g3.PINNED_DIRNAME, ".game_loop")
        os.makedirs(_pind)
        check("...a pin marker with no VERSION file is still PINNED — the tree is running other "
              "code and the sha merely unknown, which is not the same as unpinned",
              _g3.pin_status(_u3h) == (True, None))
        with open(os.path.join(_pind, "VERSION"), "w") as f:
            f.write("  deadbeef1234  \n")
        check("...and with a VERSION it reports the sha it is pinned to, stripped — so the two "
              "answers above are verdicts rather than one constant",
              _g3.pin_status(_u3h) == (True, "deadbeef1234"))

        # running_host_version — which Claude Code binary is actually running. Its NOTHING arm
        # carries a reason, and the reasons differ: unset is a different fact from set-but-shapeless.
        _saved = os.environ.pop("CLAUDE_CODE_EXECPATH", None)
        try:
            _v, _why = _g3.running_host_version()
            check("running_host_version with no CLAUDE_CODE_EXECPATH reports no version AND says "
                  "why — a version nobody could read is not version 'none'",
                  _v is None and "no CLAUDE_CODE_EXECPATH" in _why)
            os.environ["CLAUDE_CODE_EXECPATH"] = "/opt/claude/nonsense/cli.js"
            _v2, _why2 = _g3.running_host_version()
            check("...set but carrying no version is a DIFFERENT nothing, with its own reason — "
                  "the two silences are not interchangeable",
                  _v2 is None and "carries no version" in _why2 and _why2 != _why)
            # the real shape: the installer's directory carries the version in its NAME, which is
            # why this is read from the path instead of by forking the binary
            os.environ["CLAUDE_CODE_EXECPATH"] = "/opt/claude-code-1.2.34/cli.js"
            _v3, _why3 = _g3.running_host_version()
            check("...and a path carrying a version yields it, so the two nothings above are "
                  "verdicts and not a function that can only decline",
                  _v3 == "1.2.34")
        finally:
            os.environ.pop("CLAUDE_CODE_EXECPATH", None)
            if _saved is not None:
                os.environ["CLAUDE_CODE_EXECPATH"] = _saved

        # _upstream_fetch — the watcher's only network path. Its failure arm is load-bearing far
        # beyond itself: `None` is what makes an unchecked repo's baseline HOLD, so a version that
        # returned an empty dict on failure would silently convert every outage into "nothing moved
        # here" and advance the baseline over it.
        _fake = os.path.join(_up3, "bin")
        os.makedirs(_fake, exist_ok=True)
        _issues, _rel, _why4 = None, None, None
        _env0 = os.environ.get("PATH", "")
        try:
            os.environ["PATH"] = _fake          # a PATH with no `gh` on it at all
            _issues, _rel, _why4 = _g3._upstream_fetch("o/a", timeout=10)
        finally:
            os.environ["PATH"] = _env0
        check("_upstream_fetch with no `gh` reachable returns None for the issue set — NOT an "
              "empty one. That distinction is what holds an unchecked repo's baseline still, so "
              "an outage cannot be recorded as 'nothing moved here'",
              _issues is None and _why4)
        with open(os.path.join(_fake, "gh"), "w") as f:
            f.write("#!/bin/sh\nexit 4\n")     # present, and failing
        os.chmod(os.path.join(_fake, "gh"), 0o755)
        try:
            os.environ["PATH"] = _fake
            _i5, _r5, _why5 = _g3._upstream_fetch("o/a", timeout=10)
        finally:
            os.environ["PATH"] = _env0
        check("...a `gh` that runs and FAILS is also a None, with its own reason — could-not-look "
              "never collapses into looked-and-found-nothing",
              _i5 is None and _why5)
        with open(os.path.join(_fake, "gh"), "w") as f:
            f.write('#!/bin/sh\ncase "$*" in *"release list"*) echo \'[{"tagName":"v9"}]\';; '
                    '*) echo \'[{"number":5,"title":"t","updatedAt":"T1"}]\';; esac\n')
        os.chmod(os.path.join(_fake, "gh"), 0o755)
        try:
            os.environ["PATH"] = _fake
            _i6, _r6, _why6 = _g3._upstream_fetch("o/a", timeout=10)
        finally:
            os.environ["PATH"] = _env0
        check("...and a `gh` that answers yields the issues and the release, so both refusals "
              "above are verdicts rather than a fetch that can only fail",
              _why6 is None and _i6 == {"5": {"updatedAt": "T1", "title": "t"}} and _r6 == "v9")
    finally:
        os.environ.pop("GAME_LOOP_HOME", None)
        shutil.rmtree(_up3, ignore_errors=True)

    # ---- retro_nudge: floor 3, measured 1, neutering it killed nothing. Chased, not lowered. ----
    _rn = tempfile.mkdtemp(prefix="gl_nudge_")
    try:
        _rnh = os.path.join(_rn, ".game_loop")
        shutil.copytree(os.path.join(REPO, ".game_loop"), _rnh,
                        ignore=shutil.ignore_patterns("sessions", "log.jsonl", "state.json",
                                                      "upstream.json", "config.local.json",
                                                      "triggers.json", "triggers.d"))
        _rl = importlib.machinery.SourceFileLoader("gl_rn", os.path.join(_rnh, "bin", "game_loop"))
        os.environ["GAME_LOOP_HOME"] = _rnh
        _gr = importlib.util.module_from_spec(importlib.util.spec_from_loader("gl_rn", _rl))
        _rl.exec_module(_gr)
        os.environ.pop("GAME_LOOP_HOME", None)

        check("retro_nudge stays SILENT with no work since the last retro — the decline arm, "
              "without which every assertion below is satisfied by a function that always fires",
              _gr.retro_nudge({"trans_since_stepback": 0, "work_since_stepback": 0}) is None)
        check("...and stays silent one short of BOTH thresholds, so the boundary is asserted "
              "rather than the middle of the range",
              _gr.retro_nudge({"trans_since_stepback": 11, "work_since_stepback": 7}) is None)

        _w = _gr.retro_nudge({"trans_since_stepback": 0, "work_since_stepback": 8})
        check("EVIDENCE WORK alone fires it at 8 — this counter exists because the transitions one "
              "counted a verb nobody ran, so a nudge that needs `trans` is the original bug",
              _w and "evidence work" in _w and "stepback" in _w)
        _t = _gr.retro_nudge({"trans_since_stepback": 12, "work_since_stepback": 0})
        check("...and TRANSITIONS alone fires it at 12, so either counter reaches the nudge and "
              "neither is load-bearing on its own",
              _t and "transitions" in _t)
        check("...and the two say DIFFERENT things, so a reader can tell which counter tripped "
              "rather than being told a retro is due for reasons unknown",
              _t != _w)

        # THE THRESHOLDS ARE CONFIGURABLE, and a nudge that ignored config would be one nobody could
        # tune to their own cadence — which is how a warning that fires too often gets ignored.
        _cf = os.path.join(_rnh, "config.json")
        with open(_cf) as f:
            _orig_cfg = f.read()
        try:
            with open(_cf, "w") as f:
                json.dump({**json.loads(_orig_cfg), "work_nudge_every": 3}, f)
            _gr.config.cache_clear() if hasattr(_gr.config, "cache_clear") else None
            check("a lowered work_nudge_every fires the nudge earlier — the threshold is read from "
                  "config, not compiled in",
                  bool(_gr.retro_nudge({"trans_since_stepback": 0, "work_since_stepback": 3})))
        finally:
            with open(_cf, "w") as f:
                f.write(_orig_cfg)
    finally:
        os.environ.pop("GAME_LOOP_HOME", None)
        shutil.rmtree(_rn, ignore_errors=True)

    # ---- `confidence --mark` must not END on a success banner for refs that are still local ----
    _mp = tempfile.mkdtemp(prefix="gl_markpush_")
    try:
        _mph = os.path.join(_mp, ".game_loop")
        shutil.copytree(os.path.join(REPO, ".game_loop"), _mph,
                        ignore=shutil.ignore_patterns("sessions", "log.jsonl", "state.json",
                                                      "upstream.json", "config.local.json",
                                                      "triggers.json", "triggers.d"))
        _mpl = importlib.machinery.SourceFileLoader("gl_mp", os.path.join(_mph, "bin", "game_loop"))
        os.environ["GAME_LOOP_HOME"] = _mph
        _gm = importlib.util.module_from_spec(importlib.util.spec_from_loader("gl_mp", _mpl))
        _mpl.exec_module(_gm)
        os.environ.pop("GAME_LOOP_HOME", None)

        _seen = {}

        def _fake(ref, timeout=20):
            return _seen.get(ref)

        _real, _gm.remote_has_ref = _gm.remote_has_ref, _fake
        try:
            _seen = {"stable-abc": True, "stable": True}
            _ok = "\n".join(_gm.mark_publication_state("stable-abc", "stable"))
            check("with both refs on the remote, the mark's last word says consumers can reach it",
                  "BOTH refs" in _ok and "ONLY LOCAL" not in _ok)

            _seen = {"stable-abc": False, "stable": True}
            _bad = "\n".join(_gm.mark_publication_state("stable-abc", "stable"))
            check("an immutable tag that never left this machine is named as STILL ONLY LOCAL, and "
                  "the release announced above it is called what it is — about a commit the remote "
                  "cannot serve",
                  "ONLY LOCAL" in _bad and "stable-abc" in _bad
                  and "cannot serve" in _bad and "git push origin stable-abc" in _bad)

            _seen = {"stable-abc": True, "stable": False}
            _ptr = "\n".join(_gm.mark_publication_state("stable-abc", "stable"))
            check("...and a pushed tag with an UNPUSHED channel pointer is still a failure — that "
                  "is the case where consumers installing the channel get the PREVIOUS release",
                  "ONLY LOCAL" in _ptr and "channel pointer" in _ptr)

            # THREE OUTCOMES. `ls-remote` failing offline must never render as "pushed" — that is
            # the substitution this whole project exists to refuse.
            _seen = {"stable-abc": None, "stable": True}
            _unk = "\n".join(_gm.mark_publication_state("stable-abc", "stable"))
            check("a remote that could not be ASKED is its own outcome — not 'pushed', and it says "
                  "nothing was compared rather than reporting either way",
                  "COULD NOT CHECK" in _unk and "NOT 'they are pushed'" in _unk
                  and "BOTH refs" not in _unk)
        finally:
            _gm.remote_has_ref = _real

        check("...and the check is the LAST thing the mark does, after the triggers — because a "
              "publish trigger's success banner is what a reader sees last, which is how two marks "
              "here ended with the record local and the announcement already out",
              inspect.getsource(_gm.cmd_confidence).rindex("mark_publication_state")
              > inspect.getsource(_gm.cmd_confidence).rindex('fire_triggers(s, "confidence"'))
    finally:
        os.environ.pop("GAME_LOOP_HOME", None)
        shutil.rmtree(_mp, ignore_errors=True)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
