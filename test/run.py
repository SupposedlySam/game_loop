#!/usr/bin/env python3
"""game_loop's own guarantees, checked rather than remembered. Run:  python3 test/run.py

Drives the REAL scripts through their real interfaces (CLI args, stdin JSON) inside a throwaway copy
of .game_loop, so a regression in any gate fails here instead of in production. No dependencies.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

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
    for f in ("game_loop", "watchdog", "guard-writes.sh", "guard-writes-impl.sh", "verify",
              "flair.py", "notify.py"):
        shutil.copy(os.path.join(SRC_GAME_LOOP, "bin", f), os.path.join(dst, "bin", f))
        os.chmod(os.path.join(dst, "bin", f), 0o755)
    for f in ("config.json", "verify.yaml", "INVARIANTS.md"):
        shutil.copy(os.path.join(SRC_GAME_LOOP, f), os.path.join(dst, f))
    return proj


def _env(proj=None, sid=None, **extra):
    """A controlled environment: the suite itself may run inside a Claude session, and its
    CLAUDE_CODE_SESSION_ID leaking into the scripts under test would silently session-scope
    every 'legacy' check. Scrub, then set exactly what the test names."""
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("GAME_LOOP_SESSION", None)
    if proj:
        env["CLAUDE_PROJECT_DIR"] = proj
    if sid:
        env["GAME_LOOP_SESSION"] = sid
    env.update(extra)
    return env


def gl(proj, *args, stdin=None, sid=None):
    return subprocess.run([os.path.join(proj, ".game_loop", "bin", "game_loop"), *args],
                          input=stdin, capture_output=True, text=True, env=_env(sid=sid))


def guard(proj, payload, sid=None):
    return subprocess.run([os.path.join(proj, ".game_loop", "bin", "guard-writes.sh")],
                          input=json.dumps(payload), capture_output=True, text=True,
                          env=_env(proj, sid))


def denied(res):
    """The guard always exit 0; a deny is a JSON body with permissionDecision=deny."""
    return '"permissionDecision":"deny"' in res.stdout or '"permissionDecision": "deny"' in res.stdout


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
        check("allows a write inside the repo",
              not denied(guard(proj, {"tool_name": "Write", "tool_input": {"file_path": inside}})))
        check("denies a write to another dir",
              denied(guard(proj, {"tool_name": "Write",
                                  "tool_input": {"file_path": os.path.expanduser("~/evil.txt")}})))
        check("denies rm behind a cd into another tree",
              denied(guard(proj, {"tool_name": "Bash",
                                  "tool_input": {"command": "cd ~ && rm -rf somedir"}})))
        check("allows normal in-repo bash",
              not denied(guard(proj, {"tool_name": "Bash",
                                      "tool_input": {"command": "rm -f file.txt && echo hi > b.txt"}})))
        check("allows cp OUT of another tree into the repo",
              not denied(guard(proj, {"tool_name": "Bash",
                                      "tool_input": {"command": "cp ~/.bashrc ./copy"}})))
        check("allows redirecting to /dev/null (a discard device)",
              not denied(guard(proj, {"tool_name": "Bash",
                                      "tool_input": {"command": "grep x file.txt 2>/dev/null"}})))
        check("allows redirecting to a std stream (/dev/stderr)",
              not denied(guard(proj, {"tool_name": "Bash",
                                      "tool_input": {"command": "echo hi >/dev/stderr"}})))
        # #7: a DATA heredoc body (fed to cat/tee) is not executed shell — redirect-like prose in it
        # must not be flagged. But a CODE heredoc body (fed to bash/sh/...) DOES run and must stay
        # guarded, or the fix would open a bypass. Both directions are asserted.
        check("allows out-of-repo redirect text inside a cat (data) heredoc body",
              not denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "cat <<'EOF'\nnote: echo x > ~/outside.txt\nEOF"}})))
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
        check("fails OPEN when the guard impl is malformed (can't block its own fix)",
              not denied(guard(proj, {"tool_name": "Bash",
                                      "tool_input": {"command": "rm -rf ~/outside"}})))
        with open(impl_f, "w") as f:                 # restore so later checks use the real guard
            f.write(impl_src)

        print("write guard (authorize → consume):")
        gl(proj, "authorize", "--path", os.path.expanduser("~/authztest"),
               "--reason", "user said ok")
        p = {"tool_name": "Bash", "tool_input": {"command": "touch ~/authztest/x"}}
        check("authorized path allowed once", not denied(guard(proj, p)))
        check("authorization is single-use (spent → denied)", denied(guard(proj, p)))
        # #1: the escape hatch must work for the Write/Edit tools too, not just Bash mutators —
        # the deny message points at `authorize`, so `authorize` has to unblock this path.
        gl(proj, "authorize", "--path", os.path.expanduser("~/authztest-write"),
               "--reason", "user said ok")
        pw = {"tool_name": "Write",
              "tool_input": {"file_path": os.path.expanduser("~/authztest-write/x.md")}}
        check("authorized path allowed once via Write", not denied(guard(proj, pw)))
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
            return subprocess.run([wd_bin], input=json.dumps(payload), capture_output=True,
                                  text=True, env=_env(WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0"))
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
              not denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": 'git commit -m "note: echo x > ~/outside.txt"'}})))
        check("allows a deploy verb mentioned inside a commit -m message",
              not denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": 'git commit -m "docs: describe the npm publish flow"'}})))
        check("allows a redirect char inside a sed script",
              not denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "env | sed 's/=.*TOKEN.*/=<redacted>/'"}})))
        check("still denies a real redirect to a QUOTED out-of-repo target",
              denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": 'echo x > "$HOME/gl_outside.txt"'}})))
        check("still denies a deploy verb inside an interpreter arg (bash -c executes)",
              denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "bash -c 'npm publish'"}})))
        check("allows a data heredoc whose opener also has a redirect (consumer is cat, not the target)",
              not denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "cat > out.md <<'EOF'\nnote: echo x > ~/outside.txt\nEOF"}})))

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
                  not denied(guard(proj, {"tool_name": "Bash",
                                          "tool_input": {"command": "git commit -m x"},
                                          "cwd": elsewhere})))
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
              not denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": 'for source in $(find /a /b -type f -name "*.rs" 2>/dev/null); '
                             'do echo $source; done'}})))
        check("allows >/dev/stdout inside a command substitution",
              not denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "echo $(cat file.txt >/dev/stdout)"}})))
        check("allows 2>/dev/tty inside a command substitution",
              not denied(guard(proj, {"tool_name": "Bash", "tool_input": {
                  "command": "echo $(grep -c x file.txt 2>/dev/tty)"}})))
        r = guard(proj, {"tool_name": "Bash", "tool_input": {
            "command": "echo $(cat file.txt > ~/gl_paren_outside.txt)"}})
        check("still denies a real out-of-repo redirect inside a command substitution",
              denied(r) and "gl_paren_outside.txt" in r.stdout)
        check("the denied target carries no trailing paren (the offender is a usable path)",
              "gl_paren_outside.txt)" not in r.stdout)
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
        r = subprocess.run([wd_bin], input=json.dumps({"session_id": "sess-slk",
                                                       "transcript_path": tpath}),
                           capture_output=True, text=True,
                           env=_env(WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0"))
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
        r = subprocess.run([wd_bin], input=json.dumps({"session_id": "sess-slk",
                                                       "transcript_path": tpath}),
                           capture_output=True, text=True,
                           env=_env(WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0"))
        check("an unmandated session still forwards the slack reply (exit 2)",
              r.returncode == 2 and "yes deploy it" in r.stderr)

        # A human who answers at the CHANNEL top level (not in-thread) must also be caught — the natural
        # thing to do from a phone. replies() unions conversations.history with conversations.replies.
        print("watchdog forwards a top-level channel reply (not just in-thread):")
        FakeSlack.posts.clear()
        FakeSlack.thread_replies = [{"ts": "111.222", "text": "parent"}]   # NO in-thread human reply
        FakeSlack.channel_msgs = [{"ts": "111.777", "user": "U1", "text": "answer in the channel"}]
        gl(proj, "arm", "--question", "channel reply?", "--read", real, "--predict", "x", sid="sess-slk")
        r = subprocess.run([wd_bin], input=json.dumps({"session_id": "sess-slk",
                                                       "transcript_path": tpath}),
                           capture_output=True, text=True,
                           env=_env(WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0"))
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

        print("watchdog parks at an exhausted limit and rings at reset:")
        gl(proj, "mandate", "--set", "limit park work", sid="sess-park")
        json.dump({"captured_at": _t.time(),
                   "windows": {"five_hour": {"used_percentage": 99.5,
                                             "resets_at": _t.time() + 2}}},
                  open(limits_f, "w"))
        FakeSlack.posts.clear()
        r = subprocess.run([wd_bin], input=json.dumps({"session_id": "sess-park",
                                                       "transcript_path": tpath}),
                           capture_output=True, text=True,
                           env=_env(WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0",
                                    WATCHDOG_RESUME_BUFFER_SEC="0"))
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
            def do_GET(self):
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
            r = gl(up, "checkpoint", "--notes", "still on the parser")
            check("up to date with upstream → checkpoint stays quiet",
                  r.returncode == 0 and "UNPUSHED" not in r.stdout)
            for f in ("b.txt", "c.txt", "d.txt"):
                ucommit(f)
            r = gl(up, "checkpoint", "--notes", "still on the parser")
            check("ahead of upstream → checkpoint warns, and says by how much",
                  r.returncode == 0 and "UNPUSHED" in r.stdout and "3 commits" in r.stdout)
            check("the unpushed warning never blocks the checkpoint",
                  "✓ CHECKPOINT" in r.stdout)
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
        # pins are per-session state like everything else — one session's pins never leak into another
        gl(proj, "pin", "--fact", "sess-pin-A only", "--reason", "A's build needs it",
           "--path", head_f, sid="sess-pin-a")
        check("a pin registered in one session does not appear in another's status",
              "sess-pin-A only" not in gl(proj, "status", sid="sess-pin-b").stdout
              and "sess-pin-A only" in gl(proj, "status", sid="sess-pin-a").stdout)
        r = gl(proj, "pin", "--release", "p1")
        check("refuses to release a pin without --notes (the revert must stay a stated decision)",
              r.returncode != 0 and "GAMELOOP ✗" in r.stderr)
        r = gl(proj, "pin", "--release", "p1", "--notes", "the API landed on the default branch")
        check("releases a pin by id", r.returncode == 0 and "RELEASED" in r.stdout)
        r = gl(proj, "status")   # a bare absence would pass against an unimplemented verb, so this
        check("a released pin is gone from status",  # asserts the empty-pins line is present too
              "dep-checkout is pinned to abc123def456" not in r.stdout and "pins: none" in r.stdout)
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
        r = subprocess.run([wd_bin], input=json.dumps({"session_id": pk, "transcript_path": tpath}),
                           capture_output=True, text=True,
                           env=_env(WATCHDOG_IDLE_SEC="1", WATCHDOG_SETTLE_SEC="0"))
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
        with open(os.path.join(proj, ".game_loop", "log.jsonl")) as f:
            log = f.read()
        check("both probes are recorded, in the log a later session inherits",
              '"probes": ["events", "orders"]' in log)
        check("the category they belong to is recorded with them",
              '"scope": "tables you can delete from"' in log)
        # The detector is a NUDGE, never a gate: enforcement that depends on reading English is not
        # enforcement (INV1), and a guard that blocked on a false positive would block its own fix
        # (INV5). So a set-shaped assertion filed as an instance is admitted — and made loud.
        r = gl(proj, "claim", "--assert", "deletes are restricted on the events table", "--read", real)
        check("a category-shaped assertion filed as an instance is FLAGGED, not blocked",
              r.returncode == 0 and "reads category-shaped" in r.stdout)
        check("the nudge names the workaround as the tell",
              "WORKAROUND" in r.stdout)
        r = gl(proj, "claim", "--assert", "the retry helper sleeps 2s between attempts",
               "--read", real)
        check("an ordinary instance claim is untouched — no nudge, nothing owed",
              r.returncode == 0 and "CLAIM sourced" in r.stdout
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
    finally:
        shutil.rmtree(proj, ignore_errors=True)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
