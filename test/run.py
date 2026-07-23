#!/usr/bin/env python3
"""bumper's own guarantees, checked rather than remembered. Run:  python3 test/run.py

Drives the REAL scripts through their real interfaces (CLI args, stdin JSON) inside a throwaway copy
of .bumper, so a regression in any gate fails here instead of in production. No dependencies.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_BUMPER = os.path.join(REPO, ".bumper")

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
    """A temp project with a fresh .bumper (real scripts, empty state)."""
    proj = tempfile.mkdtemp(prefix="bumper-test-")
    dst = os.path.join(proj, ".bumper")
    os.makedirs(os.path.join(dst, "bin"))
    for f in ("bumper", "watchdog", "guard-writes.sh", "verify", "flair.py"):
        shutil.copy(os.path.join(SRC_BUMPER, "bin", f), os.path.join(dst, "bin", f))
        os.chmod(os.path.join(dst, "bin", f), 0o755)
    for f in ("config.json", "verify.yaml", "INVARIANTS.md"):
        shutil.copy(os.path.join(SRC_BUMPER, f), os.path.join(dst, f))
    return proj


def bumper(proj, *args, stdin=None):
    return subprocess.run([os.path.join(proj, ".bumper", "bin", "bumper"), *args],
                          input=stdin, capture_output=True, text=True)


def guard(proj, payload):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=proj)
    return subprocess.run([os.path.join(proj, ".bumper", "bin", "guard-writes.sh")],
                          input=json.dumps(payload), capture_output=True, text=True, env=env)


def denied(res):
    """The guard always exit 0; a deny is a JSON body with permissionDecision=deny."""
    return '"permissionDecision":"deny"' in res.stdout or '"permissionDecision": "deny"' in res.stdout


def main():
    proj = make_sandbox()
    try:
        print("claim gate:")
        check("refuses a nonexistent --read path",
              bumper(proj, "claim", "--assert", "x", "--read", "/no/such/file").returncode != 0)
        real = os.path.join(proj, ".bumper", "config.json")
        check("accepts a real --read path",
              bumper(proj, "claim", "--assert", "x", "--read", real).returncode == 0)

        print("stop gate (no mandate = inert):")
        r = bumper(proj, "stopgate", stdin='{"last_assistant_message":"want me to continue?"}')
        check("inert with no mandate → allows", r.returncode == 0)

        print("stop gate (mandate bound):")
        bumper(proj, "mandate", "--set", "do the work")
        r = bumper(proj, "stopgate", stdin='{"last_assistant_message":"Done part 1. Want me to do part 2?"}')
        check("blocks a question (exit 2)", r.returncode == 2)
        r = bumper(proj, "stopgate", stdin='{"last_assistant_message":"Continuing to the next step now."}')
        check("blocks announce-then-stop (exit 2)", r.returncode == 2)
        bumper(proj, "checkpoint", "--notes", "reporting")
        r = bumper(proj, "stopgate", stdin='{"last_assistant_message":"Finished the batch. Remaining: docs."}')
        check("allows a checkpointed report (exit 0)", r.returncode == 0)
        # checkpoint is consumed — a second bare stop blocks again
        r = bumper(proj, "stopgate", stdin='{"last_assistant_message":"Still going on the batch."}')
        check("checkpoint is single-use (next bare stop blocks)", r.returncode == 2)

        print("stop gate (arm → consume):")
        bumper(proj, "arm", "--question", "which color?", "--read", real, "--predict", "blue")
        r = bumper(proj, "stopgate", stdin='{"last_assistant_message":"Which color do you want?"}')
        check("armed question passes once (exit 0)", r.returncode == 0)
        r = bumper(proj, "stopgate", stdin='{"last_assistant_message":"And which size?"}')
        check("arm is consumed (next question blocks)", r.returncode == 2)
        bumper(proj, "mandate", "--clear", "--notes", "done")

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

        print("write guard (authorize → consume):")
        bumper(proj, "authorize", "--path", os.path.expanduser("~/authztest"),
               "--reason", "user said ok")
        p = {"tool_name": "Bash", "tool_input": {"command": "touch ~/authztest/x"}}
        check("authorized path allowed once", not denied(guard(proj, p)))
        check("authorization is single-use (spent → denied)", denied(guard(proj, p)))

        print("deploy verbs:")
        cf = os.path.join(proj, ".bumper", "config.json")
        c = json.load(open(cf)); c["deploy_verbs"] = ["firebase deploy"]
        json.dump(c, open(cf, "w"))
        check("blocks a configured deploy verb",
              denied(guard(proj, {"tool_name": "Bash",
                                  "tool_input": {"command": "firebase deploy --only hosting"}})))

        print("flair:")
        # a claim emits a fun line; a milestone (10 claims) fires the coffee-adjacent shout-out
        r = bumper(proj, "claim", "--assert", "x", "--read", real)
        check("a claim prints a flair line", "🎳" in r.stdout)
        for _ in range(12):                            # cross the 10-claim milestone
            bumper(proj, "claim", "--assert", "x", "--read", real)

        def fired(p):
            return json.load(open(os.path.join(p, ".bumper", "state.json"))).get("flair_fired", [])
        check("10-claim milestone fires exactly once", fired(proj).count("claim:10") == 1)
        bumper(proj, "status"); bumper(proj, "status")
        check("a fired milestone does not repeat", fired(proj).count("claim:10") == 1)
        # funding CTAs rotate — sample several uptime fires and confirm the wording varies
        c = json.load(open(cf)); c["mandate"] = None
        # bind a mandate backdated far enough to cross many uptime milestones at once
        import datetime as _dt
        bumper(proj, "mandate", "--set", "long run")
        st = json.load(open(os.path.join(proj, ".bumper", "state.json")))
        st["mandate"]["since"] = (_dt.datetime.now() - _dt.timedelta(hours=200)).isoformat(timespec="seconds")
        json.dump(st, open(os.path.join(proj, ".bumper", "state.json"), "w"))
        r = bumper(proj, "status")
        ctas = [ln for ln in r.stdout.splitlines() if "☕" in ln]
        check("many uptime milestones fire with a coffee CTA", len(ctas) >= 5)
        check("the funding CTA wording varies (not all identical)", len(set(ctas)) >= 2)
        # flair.enabled=false silences it
        c = json.load(open(cf)); c["flair"] = {"enabled": False}; json.dump(c, open(cf, "w"))
        r = bumper(proj, "claim", "--assert", "x", "--read", real)
        check("flair.enabled=false silences flair", "🎳" not in r.stdout)
        c = json.load(open(cf)); c.pop("flair", None); json.dump(c, open(cf, "w"))
    finally:
        shutil.rmtree(proj, ignore_errors=True)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
