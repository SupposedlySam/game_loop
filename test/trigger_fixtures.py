"""Exercise `triggers.d` scripts against fixtures, in both directions. Run:  python3 test/trigger_fixtures.py

Prompted by #87 — filed after the same session had a trigger match a log `kind` nothing ever
writes, AND wrote the trigger's own test fixtures from that same wrong mental model, so a green
suite passed while the guard was dead. `game_loop kinds` and the dead-kind check in `status` now
catch (1). This file is the other half: a trigger whose fixture agrees with its own bug still
passes a suite that only checks the fixture against itself, so the fixture has to be checked
against something outside the trigger too.

TWO PROPERTIES DO THE REAL WORK:

  1. EVERY TRIGGER NEEDS A FIRING CASE AND A QUIET CASE, WITH NO EXEMPTIONS. Not "the ones I'm
     unsure about" — every one. A trigger that only has a case for the condition it was built to
     catch has never been shown to leave correct behaviour alone.

  2. WEIGHT THE TWO DIRECTIONS DIFFERENTLY, because their failure costs are not symmetric. A false
     quiet costs one missed catch. A false firing costs the gate ENTIRELY — a check that blocks
     legitimate work gets routed around within a day, and stays disabled long after the false
     positive is fixed. Quiet cases outnumber firing ones below for exactly this reason.

THE LESSON UNDERNEATH BOTH: a fixture written by the author of the bug encodes the bug. A trigger
and its test, authored from the same wrong model, agree with each other and both disagree with
reality — a green suite in that state is not a second opinion, it is the same opinion twice. So
every case here runs the REAL script (real stdin, real exit code, real stderr) rather than
asserting against a description of what it is supposed to do, and the log-based fixture checks its
own `kind` values against `log_kinds()` (#87) rather than a hand-maintained list, for the same
reason the trigger it is testing does.

FOUR FIXTURE SHAPES, one per way a `triggers.d` script reads the world:

  1. a synthetic `$GAME_LOOP_ROOT/log.jsonl`                        — `example-harden-without-claim.sh`
  2. a throwaway git repo, with/without a configured upstream       — `example-unpushed-at-stop.sh`
  3. an external command stubbed onto PATH (generalises `GH_BIN`)   — the shipped `example-answer-owed`
  4. a repo with an `origin/main` and a feature branch diffed against it

Shape 4 is provided as infrastructure and self-tested only: nothing shipped in this repo reads a
diff against a base ref yet, and writing a gate just to exercise the fixture would be exactly the
"fixture-shaped, not project-shaped" problem this file exists to avoid on the other three.

WHAT THIS DOES NOT COVER: the pluggable-attachment MECHANISM itself (timeouts, the 3-consecutive
stand-down, failing open on a crash) — that is `stop_trigger_block` in `bin/game_loop`, and
`test/run.py` already drives it end to end. This file is about the SCRIPTS a project attaches,
not the harness that runs them.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES_DIR = os.path.join(REPO, "templates", "triggers.d-examples")
GAME_LOOP_SRC = os.path.join(REPO, ".game_loop", "bin", "game_loop")

passed = 0
failed = 0

# name -> {"fired": bool, "quiet": bool} — the meta-check at the bottom refuses to pass unless
# every registered trigger has BOTH, which is property (1) enforced on this file rather than
# merely stated in its docstring.
COVERAGE = {}


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok    {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


def record(trigger, fired):
    c = COVERAGE.setdefault(trigger, {"fired": False, "quiet": False})
    c["fired" if fired else "quiet"] = True


# ── fixture shape 1: a synthetic log ────────────────────────────────────────────────────────────

def write_log(gl_root, lines):
    os.makedirs(gl_root, exist_ok=True)
    with open(os.path.join(gl_root, "log.jsonl"), "w") as f:
        for line in lines:
            f.write(line + "\n")


def run_log_trigger(script, lines, session=""):
    """Run a `triggers.d` script against a synthetic log. Returns (exit_code, stdout, stderr)."""
    work = tempfile.mkdtemp(prefix="gl_fixture_log_")
    try:
        gl_root = os.path.join(work, ".game_loop")
        write_log(gl_root, lines)
        env = dict(os.environ, GAME_LOOP_ROOT=gl_root, GAME_LOOP_SESSION=session)
        r = subprocess.run([os.path.join(EXAMPLES_DIR, script)], env=env,
                            capture_output=True, text=True, timeout=10)
        return r.returncode, r.stdout, r.stderr
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── fixture shape 2: a throwaway git repo ───────────────────────────────────────────────────────

def _git(cwd, *args):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {r.stderr}")
    return r.stdout


def make_git_repo():
    """A throwaway repo with one commit and no remote. Returns its path."""
    repo = tempfile.mkdtemp(prefix="gl_fixture_git_")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    return repo


def add_pushed_upstream(repo):
    """Gives `repo` an `origin` with its current HEAD already pushed — "fully up to date"."""
    origin = tempfile.mkdtemp(prefix="gl_fixture_origin_")
    _git(origin, "init", "-q", "--bare")
    _git(repo, "remote", "add", "origin", origin)
    _git(repo, "push", "-q", "-u", "origin", "HEAD:main")
    return origin


def run_git_trigger(script, repo):
    env = dict(os.environ, GAME_LOOP_REPO=repo)
    r = subprocess.run([os.path.join(EXAMPLES_DIR, script)], env=env,
                        capture_output=True, text=True, timeout=10)
    return r.returncode, r.stdout, r.stderr


# ── fixture shape 3: an external command stubbed onto PATH ─────────────────────────────────────
# Generalises the "point `gh` at a fake binary via `GH_BIN`" convention: a `triggers.d` script can shell out to
# ANY external tool, not just `gh`, so the fixture stubs whatever name the script actually invokes
# rather than special-casing one binary.

def stub_command(bindir, name, script_body):
    path = os.path.join(bindir, name)
    with open(path, "w") as f:
        f.write("#!/usr/bin/env bash\n" + script_body + "\n")
    os.chmod(path, 0o755)


def run_stubbed_trigger(command, stub_name, stub_body, payload=None):
    """Run a raw shell `command` (e.g. copied from templates/triggers.example.json) with `stub_name`
    stubbed onto PATH ahead of the real PATH, and `payload` (a dict, or None) fed on stdin exactly
    like the real stdin-JSON contract triggers get."""
    bindir = tempfile.mkdtemp(prefix="gl_fixture_bin_")
    try:
        stub_command(bindir, stub_name, stub_body)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""),
                   GAME_LOOP_SESSION="fixturesess")
        stdin = json.dumps(payload) if payload is not None else ""
        r = subprocess.run(["bash", "-c", command], input=stdin, env=env,
                            capture_output=True, text=True, timeout=10)
        return r.returncode, r.stdout, r.stderr
    finally:
        shutil.rmtree(bindir, ignore_errors=True)


# ── fixture shape 4: a repo with an origin/main and a feature branch diffed against it ─────────
# Infrastructure only (see module docstring) — self-tested below, not used to back a fabricated gate.

def make_repo_with_origin_and_branch(base_files, branch_files):
    """`base_files`/`branch_files`: {relative path: contents}. Returns (repo_path, branch_name)."""
    repo = tempfile.mkdtemp(prefix="gl_fixture_floor_")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    for rel, contents in base_files.items():
        full = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
        with io.open(full, "w") as f:
            f.write(contents)
        _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", "base")
    # origin/main without an actual remote: a diff against it only ever needs the ref to resolve.
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    branch = "feature-probe"
    _git(repo, "checkout", "-q", "-b", branch)
    for rel, contents in branch_files.items():
        full = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
        with io.open(full, "w") as f:
            f.write(contents)
        _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", "measured")
    return repo, branch


def diff_added_lines(repo, path):
    return _git(repo, "diff", "origin/main", "--", path)


# ── log_kinds(): imported straight from the source, never transcribed (#87) ────────────────────
# Mirrors test/run.py's own import of this file for the same reason: an isolated copy under a
# throwaway GAME_LOOP_HOME so importing it cannot read or write this checkout's real state.

def load_log_kinds():
    import importlib.machinery
    import importlib.util
    home = tempfile.mkdtemp(prefix="gl_fixture_kinds_")
    try:
        dst = os.path.join(home, "game_loop")
        shutil.copytree(os.path.join(REPO, ".game_loop"), dst,
                         ignore=shutil.ignore_patterns(
                             "sessions", "log.jsonl", "state.json", "upstream.json",
                             "config.local.json", "triggers.json", "triggers.d",
                             "UPSTREAM_LEDGER.md", ".game_loop_self"))
        loader = importlib.machinery.SourceFileLoader(
            "gl_fixtures_kinds", os.path.join(dst, "bin", "game_loop"))
        os.environ["GAME_LOOP_HOME"] = dst
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("gl_fixtures_kinds", loader))
        loader.exec_module(mod)
        return mod.log_kinds()
    finally:
        os.environ.pop("GAME_LOOP_HOME", None)
        shutil.rmtree(home, ignore_errors=True)


def main():
    print("fidelity — every kind the log-based example reads is one this codebase actually writes:")
    kinds = load_log_kinds()
    used = ("mandate_set", "mandate_clear", "mandate_park", "claim", "harden", "trans")
    for k in used:
        check(f"'{k}' appears in the extracted schema (game_loop kinds)", k in kinds)

    print()
    print("example-harden-without-claim.sh — a mandate's work with nothing claimed to back it:")
    SET = '{"kind":"mandate_set","t":"2026-01-01T00:00:00Z","text":"do the thing"}'
    HARDEN = '{"kind":"harden","t":"2026-01-01T00:01:00Z","learning":"x","artifact":"y","mechanism":"z","rung":3}'
    CLAIM = '{"kind":"claim","t":"2026-01-01T00:02:00Z","assert":"x","read":"y","confidence":"z"}'
    CLEAR = '{"kind":"mandate_clear","t":"2026-01-01T00:02:00Z","notes":"done"}'

    code, _, err = run_log_trigger("example-harden-without-claim.sh", [SET, HARDEN])
    check("FIRING — a harden with no claim, mandate still bound, blocks the turn",
          code != 0 and "no claims" in err)
    record("example-harden-without-claim.sh", fired=True)

    code, _, _ = run_log_trigger("example-harden-without-claim.sh", [SET, HARDEN, CLAIM])
    check("quiet — the same harden, but a claim exists", code == 0)
    record("example-harden-without-claim.sh", fired=False)

    code, _, _ = run_log_trigger("example-harden-without-claim.sh", [SET])
    check("quiet — a mandate with no work done under it yet", code == 0)
    record("example-harden-without-claim.sh", fired=False)

    code, _, _ = run_log_trigger("example-harden-without-claim.sh", [SET, HARDEN, CLEAR])
    check("quiet — the harden stands, but the mandate was cleared before this turn-end", code == 0)
    record("example-harden-without-claim.sh", fired=False)

    code, _, _ = run_log_trigger("example-harden-without-claim.sh", [HARDEN])
    check("quiet — no mandate was ever bound at all, so nothing is owed", code == 0)
    record("example-harden-without-claim.sh", fired=False)

    print()
    print("example-unpushed-at-stop.sh — commits this checkout's upstream has never seen:")
    repo = make_git_repo()
    code, _, _ = run_git_trigger("example-unpushed-at-stop.sh", repo)
    check("quiet — no upstream configured at all: cannot tell, must not guess 'fully pushed'",
          code == 0)
    record("example-unpushed-at-stop.sh", fired=False)

    origin = add_pushed_upstream(repo)
    code, _, _ = run_git_trigger("example-unpushed-at-stop.sh", repo)
    check("quiet — upstream configured and HEAD already matches it", code == 0)
    record("example-unpushed-at-stop.sh", fired=False)

    _git(repo, "commit", "-q", "--allow-empty", "-m", "local only")
    code, _, err = run_git_trigger("example-unpushed-at-stop.sh", repo)
    check("FIRING — one commit ahead of the pushed upstream blocks the turn",
          code != 0 and "not on origin/main" in err)
    record("example-unpushed-at-stop.sh", fired=True)
    shutil.rmtree(repo, ignore_errors=True)
    shutil.rmtree(origin, ignore_errors=True)

    print()
    print("the shipped example-answer-owed (templates/triggers.example.json) — a question nobody "
          "in the room has answered yet:")
    with open(os.path.join(REPO, "templates", "triggers.example.json")) as f:
        example_json = json.load(f)
    owed_entry = next(t for t in example_json["stop"] if isinstance(t, dict)
                       and t.get("name") == "example-answer-owed")
    OWED_CMD = owed_entry["command"]

    code, out_, err = run_stubbed_trigger(
        OWED_CMD, "your-chat-tool",
        'echo "does this migration need a backfill?"')
    check("FIRING — the stubbed tool reports something owed, and the turn is blocked",
          code != 0 and "have not answered it" in err)
    record("example-answer-owed", fired=True)

    code, _, _ = run_stubbed_trigger(OWED_CMD, "your-chat-tool", 'true')
    check("quiet — the stubbed tool succeeds and reports nothing owed", code == 0)
    record("example-answer-owed", fired=False)

    code, _, _ = run_stubbed_trigger(OWED_CMD, "your-chat-tool", 'exit 1')
    check("quiet — the stubbed tool itself fails (room unreachable): fails open, not closed",
          code == 0)
    record("example-answer-owed", fired=False)

    print()
    print("fixture shape 4 (repo + origin/main + feature branch) — self-test, infrastructure only:")
    repo, branch = make_repo_with_origin_and_branch(
        base_files={"README": "hello\n"},
        branch_files={"README": "hello\nworld\n"})
    diff = diff_added_lines(repo, "README")
    check("the fixture repo actually shows the branch's line as added against origin/main",
          "+world" in diff)
    check("...and current branch is the feature branch, not main",
          _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == branch)
    shutil.rmtree(repo, ignore_errors=True)

    print()
    print("meta — every trigger tested above has both a firing case and a quiet case:")
    for name, cov in sorted(COVERAGE.items()):
        check(f"{name}: {'firing+quiet' if cov['fired'] and cov['quiet'] else 'INCOMPLETE'}",
              cov["fired"] and cov["quiet"])

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
