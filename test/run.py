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
    for f in ("game_loop", "watchdog", "guard-writes.sh", "guard-writes-impl.sh",
              "guard-mcp.sh", "guard-mcp-impl.sh", "verify", "flair.py", "notify.py"):
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
        gl(proj, "instrument", "--register", "latency", "--measures", "waits the player notices",
           "--connects", "a long frame is a visible hitch", "--null", "0,0", "--positive", "0,8",
           sid=SD)
        gl(proj, "measure", "--instrument", "latency", "--before", "10", "--after", "40", sid=SD)
        r = gl(proj, "claim", "--assert", "latency fell 30% in total", "--metric", "latency", sid=SD)
        check("an aggregate-shaped sentence with no shape behind it is made LOUD, never refused",
              r.returncode == 0 and 'reads aggregate-shaped ("30%")' in r.stdout
              and "Wording only" in r.stdout)
        r = gl(proj, "claim", "--assert", "the counter read 40 on this build", "--metric",
               "latency", sid=SD)
        check("a plain observation claim gets no aggregate nudge — the tell stays quiet",
              r.returncode == 0 and "CLAIM sourced" in r.stdout
              and "reads aggregate-shaped" not in r.stdout)
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

            def wcommit(cwd, sid=None):
                return guard(wmain, {"tool_name": "Bash", "cwd": cwd, "session_id": sid or "",
                                     "tool_input": {"command": "git commit -m x"}}, sid=sid)

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
            r = wcommit(wt)
            check("a verified worktree commit is allowed while the main tree is stale (#28)",
                  not denied(r))

            # FALSE PASS — the defect. Main green from a run that never saw the worktree's files.
            os.utime(os.path.join(wmain, "unrelated.txt"), (_t.time() - 5, _t.time() - 5))
            wverify(wmain)
            r = wcommit(wmain)
            check("the main tree passes once its own checks have run (the control)",
                  not denied(r))
            wwrite(wt, "late.txt", +5)
            r = wcommit(wt)
            check("a STALE worktree commit is refused though the main record is green (#28)",
                  denied(r) and "VERIFY REFUSED" in r.stdout and "late.txt" in r.stdout)
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
            r = wcommit(wmain)
            check("and passes again once the project's own checks have run",
                  not denied(r))
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
                  and d["harness"]["matched"] == ["config.json", "INVARIANTS.md", "verify.yaml",
                                                  "LEDGER.md"])

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
                  and sorted(wd["harness"]["matched"] + wd["harness"]["drifted"]
                             + wd["harness"]["unreadable"])
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
    finally:
        shutil.rmtree(hp, ignore_errors=True)

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

        # THE COMMON PATH — variable unset, code and home the same directory. Every existing install
        # depends on this being untouched. These three PASS IN BOTH STATES by construction, and that
        # is precisely the claim being made about them.
        check("unset: the repo's own verify behaves exactly as before (passes in both states)",
              own.returncode == 1 and "VERIFY REFUSED" in own.stdout)
        plain = run("game_loop", "status", code=home)
        check("unset: status says nothing about pinning (passes in both states)",
              plain.returncode == 0 and "PINNED CODE" not in plain.stdout)
        pw = hook("guard-writes.sh", {"tool_name": "Write",
                                      "tool_input": {"file_path": os.path.join(sp, "f.txt")}},
                  code=home)
        check("unset: the write guard still allows an in-repo write (passes in both states)",
              not denied(pw))

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
        check("...and prints wiring that SETS the home, for settings.local.json — not the tracked one",
              'GAME_LOOP_HOME="$CLAUDE_PROJECT_DIR/.game_loop"' in r.stdout
              and "settings.local.json" in r.stdout and "NOT in the tracked" in r.stdout)
        # Measured, not predicted: settings.json and settings.local.json MERGE. Wiring the pin
        # without unwiring the tracked file ran every gate twice — a commit printed the same
        # blast-radius warning verbatim two times. It fails quietly, since duplicate output reads
        # as the tool repeating itself rather than as two copies of it running, so the instructions
        # have to say it. This pins that they do.
        check("...and says to REMOVE the tracked hooks, because the two files merge rather than override",
              "REMOVE THE GAME_LOOP HOOKS" in r.stdout and "MERGE" in r.stdout
              and "twice" in r.stdout and "install.sh ." in r.stdout)
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

    # The documented `curl | bash` one-liner could install into a FRESH directory and could never
    # UPGRADE one. Piped, BASH_SOURCE[0] is empty; the old fallback to $0 made `dirname bash` = "."
    # so SRC became the cwd. On a fresh target the file test below it failed and the fetch happened
    # anyway — which is why installing into clean directories never showed it. On an upgrade the
    # target already had .game_loop/bin/game_loop, the test passed against the target's OWN old
    # copy, the fetch was skipped, SRC == TARGET, and it died claiming you had pointed it at the
    # game_loop repo. Reported from outside with a two-arm repro; the arms are pinned here.
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

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
