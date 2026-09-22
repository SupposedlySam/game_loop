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
import re
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


# ── the selection detector, extracted so it can be driven against known-bad input ───────────────
#
# IT IS A FUNCTION RATHER THAN INLINE CODE FOR ONE REASON: a check cannot be trusted until it has
# been observed to FIRE on the defect it names, and an expression buried in a loop cannot be handed
# an input. Both versions of this detector shipped broken in consecutive commits, both read
# correctly, and both had a green suite:
#
#   * the BLOCKING half matched `^\s*exit\s+[1-9]` — line-anchored — so `[ -n "$prs" ] && exit 2`
#     was waved through. That is the commonest shell form AND the exact shape game_loop#130
#     reported, missed by the check written to catch it.
#   * the ACCOUNT half then read COMMENTS, so the example written to demonstrate the correct shape
#     was flagged for quoting the wrong one in its own explanation.
#
# Reading the code agreed with me both times. Feeding it the bad input did not.

ACCOUNT_SCOPED = re.compile(r"--author\s+[\"']?@me|--involves\s+[\"']?@me|--assignee\s+[\"']?@me")


def classify_example(body):
    """(can_exit_non_zero, selects_on_account) for one trigger script's source.

    COMMENTS ARE STRIPPED FOR BOTH HALVES. Every example here carries a CONTRACT comment about
    non-zero exits, and the #130 example must quote `--author "@me"` to say what not to do — so a
    scan over raw text either invents a blocker or convicts the documentation. wcs's rule, learned
    the hour a postmortem naming its own markers tripped the guard it was about: a guard you cannot
    write about is one somebody switches off.

    `can_exit_non_zero` is deliberately a SUPERSET of "blocks a turn-end on a judgement about your
    work" — example-open-issues.sh exits non-zero to say the tracker refused. Erring wide is right
    here, because an account-scoped script that can exit non-zero for ANY reason can still stop the
    wrong session; the name says what is measured rather than what it implies.
    """
    code = "\n".join(re.sub(r"#.*$", "", ln) for ln in body.splitlines())
    return bool(re.search(r"\bexit\s+[1-9]", code)), bool(ACCOUNT_SCOPED.search(code))


# THE PROBES THIS DETECTOR MUST PASS BEFORE ITS VERDICT ON REAL FILES MEANS ANYTHING.
# Each is a shape that actually shipped broken, kept as the input rather than as a memory of it.
DETECTOR_PROBES = (
    ("the #130 shape: blocks via `&& exit 2` and selects on the account",
     'prs=$(gh pr list --author "@me" --json number)\n[ -n "$prs" ] && exit 2\nexit 0\n',
     True, True),
    ("a line-initial exit, which the first version DID catch — so the fix widened rather than "
     "replaced",
     'if true; then\n    exit 1\nfi\n', True, False),
    ("the defect named in a COMMENT is documentation, not an invocation",
     '# never do this: gh pr list --author "@me" selects the ACCOUNT\nexit 1\n', True, False),
    ("a pure reporter that always exits 0 is not a blocker",
     'echo "3 issues open"\nexit 0\n', False, False),
)


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
        # `_gl_impl.py`, NOT the `bin/game_loop` stub. `log_kinds` moved into the implementation
        # when the CLI was split into a thin door plus _gl_impl, and this loader was never
        # repointed — the second half of the same regression as the missing __main__ guard, and
        # invisible for the same reason: nothing runs this file. It is in verify.yaml now.
        loader = importlib.machinery.SourceFileLoader(
            "gl_fixtures_kinds", os.path.join(dst, "bin", "_gl_impl.py"))
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

    # ── example-open-issues.sh — the work queue, which SHIPS because three projects rebuilt it ──
    # Audited 2026-09-17: llm_chat, showrunner and this repo had each built this attachment
    # independently, every one of them from a broadcast rather than a file, and they had drifted —
    # one 70 lines to another's 50, one having lost the acknowledgement mechanism entirely so a
    # reply its author had settled re-reported forever. A copy taken from a message never receives
    # the original's later fixes; a copy taken from templates/ upgrades with the payload.
    #
    # It REPORTS and never blocks, so "firing" here means "named work", not "exit non-zero".
    OPEN_SH = os.path.join(REPO, "templates", "triggers.d-examples", "example-open-issues.sh")
    GH_TWO = ('case "$*" in\n'
              '  *"repo view"*) echo "acme/widget" ;;\n'
              '  *"issue list"*) echo \'[{"number":7,"title":"a real one","labels":[]},'
              '{"number":9,"title":"awaiting a call","labels":[{"name":"needs-input"}]}]\' ;;\n'
              'esac')
    code, out_, _ = run_stubbed_trigger("bash %s" % OPEN_SH, "gh", GH_TWO)
    check("FIRING — actionable work is listed, and the issue WAITING ON A HUMAN is counted "
          "separately rather than listed beside it: a report that mixes them trains the reader to "
          "skim, which is how a standing queue stops being read",
          code == 0 and "#7" in out_ and "1 issue(s) actionable, 1 waiting" in out_
          and "do not guess these" in out_)
    record("example-open-issues.sh", fired=True)

    GH_NONE = ('case "$*" in\n'
               '  *"repo view"*) echo "acme/widget" ;;\n'
               '  *"issue list"*) echo "[]" ;;\n'
               'esac')
    code, out_, _ = run_stubbed_trigger("bash %s" % OPEN_SH, "gh", GH_NONE)
    check("quiet — an empty queue says it is GENUINELY empty, which is a different sentence from "
          "the could-not-look case below",
          code == 0 and "genuinely empty" in out_)
    record("example-open-issues.sh", fired=False)

    # THE ONE THAT MATTERS MOST (INV8): a tracker that refuses must NOT read as an empty queue.
    # Those two are the same bytes to a careless reader and they mean opposite things.
    GH_DOWN = ('case "$*" in\n'
               '  *"repo view"*) echo "acme/widget" ;;\n'
               '  *"issue list"*) echo "HTTP 403: rate limited"; exit 1 ;;\n'
               'esac')
    code, out_, err = run_stubbed_trigger("bash %s" % OPEN_SH, "gh", GH_DOWN)
    check("could-not-look is NOT an empty queue — the tracker refusing exits non-zero and names "
          "the refusal, where reporting zero issues would be a silent false all-clear",
          code != 0 and "could not reach" in err and "genuinely empty" not in out_)

    # AND THE REPO IS ASKED FOR, NOT HARDCODED — the single change that makes the file copyable.
    # Every instance found in the audit named its own repo in a string, so it could not move
    # between projects without an edit somebody had to remember to make.
    GH_NOREPO = 'case "$*" in\n  *"repo view"*) exit 1 ;;\n  *) echo "[]" ;;\nesac'
    code, _, err = run_stubbed_trigger("bash %s" % OPEN_SH, "gh", GH_NOREPO)
    check("...and with no resolvable repo it refuses and says so, rather than reporting the queue "
          "of whatever repo happened to be nearby",
          code != 0 and "not a GitHub checkout" in err)


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


    # ── example-own-branch-prs.sh — the MECHANISM behind the #130 rule ──────────────────────────
    #
    # The README beside these examples says a BLOCKING trigger must select on something this
    # session owns, and for a while it said only that. A rule with no mechanism is how #130
    # happened: the consumer's gate reached for the obvious selector, `--author "@me"`, and that is
    # the ACCOUNT. This example is the shape to copy instead, so the rule ships with a way to obey
    # it.
    #
    # THE FIRING AND QUIET CASES DIFFER ONLY BY WHICH BRANCH THE CHECKOUT IS STANDING ON. That is
    # the property under test and it is also the whole claim: the current branch is what actually
    # separated the two sessions in #130, which shared a checkout and a login but not a branch.
    OWN_SH = os.path.join(EXAMPLES_DIR, "example-own-branch-prs.sh")
    _own_repo = make_git_repo()
    _git(_own_repo, "checkout", "-q", "-b", "feature-a")

    # The stub answers the SESSION-SCOPED question the script asks — `--head <branch>` — rather
    # than handing back the account's PRs for the script to filter. A stub that ignored --head
    # would let a script selecting on the account pass this fixture, which is the one thing these
    # assertions must not do.
    GH_HEAD = ('case "$*" in\n'
               '  *"--head feature-a"*) echo \'[{"number":41,"title":"session A work"}]\' ;;\n'
               '  *"pr list"*) echo "[]" ;;\n'
               'esac')
    code, _o, err = run_stubbed_trigger(
        "cd %s && GAME_LOOP_REPO=%s bash %s" % (_own_repo, _own_repo, OWN_SH), "gh", GH_HEAD)
    check("FIRING — a PR headed by the branch this checkout is standing on is held against this "
          "session, and the refusal NAMES the branch that made it ours",
          code != 0 and "#41" in err and "feature-a" in err)
    check("...and the refusal says NOT to touch the marker when the PR is not yours, because the "
          "marker means 'I did the work' — that invitation to lie is the whole of #130, and a "
          "narrower selector does not remove it for two sessions sharing one branch",
          "do NOT touch the marker" in err and "Tell the session that opened it" in err)
    record("example-own-branch-prs.sh", fired=True)

    # SAME REPO, SAME STUB, SAME ACCOUNT — only the branch moves. Under `--author "@me"` this case
    # is indistinguishable from the one above, which is exactly the defect being fixed.
    _git(_own_repo, "checkout", "-q", "-b", "feature-b")
    code, _o, err = run_stubbed_trigger(
        "cd %s && GAME_LOOP_REPO=%s bash %s" % (_own_repo, _own_repo, OWN_SH), "gh", GH_HEAD)
    check("quiet — another session's PR on a DIFFERENT branch is not this session's to answer for. "
          "Only the branch changed between this and the case above; under an account selector the "
          "two are the same call, and #130 is what that costs",
          code == 0 and "#41" not in err)
    record("example-own-branch-prs.sh", fired=False)

    # COULD-NOT-LOOK IS NOT "NO PR" (INV8). A tracker that refuses and a branch with no PR are the
    # same empty bytes, and treating them alike makes a blocking gate silently stop blocking.
    GH_REFUSE = 'echo "HTTP 403: rate limited" >&2; exit 1'
    _git(_own_repo, "checkout", "-q", "feature-a")
    code, _o, err = run_stubbed_trigger(
        "cd %s && GAME_LOOP_REPO=%s bash %s" % (_own_repo, _own_repo, OWN_SH), "gh", GH_REFUSE)
    check("a tracker that REFUSES fails open rather than blocking on a question it could not ask "
          "— the opposite trade from the report-only example above, and correct for each: a gate "
          "must never block its own fix, while a report that cannot look must not claim silence",
          code == 0)

    # DETACHED HEAD OWNS NOTHING. `rev-parse --abbrev-ref HEAD` answers the literal string "HEAD"
    # there, which is not a branch and would have been compared against one.
    _git(_own_repo, "checkout", "-q", "--detach")
    code, _o, _e = run_stubbed_trigger(
        "cd %s && GAME_LOOP_REPO=%s bash %s" % (_own_repo, _own_repo, OWN_SH), "gh", GH_HEAD)
    check("a DETACHED head owns no branch and therefore no PR — git answers the literal string "
          "'HEAD' there, which would otherwise have been sent to the tracker as a branch name",
          code == 0)
    shutil.rmtree(_own_repo, ignore_errors=True)

    print()
    print("selection: a BLOCKING example may not decide whose work it is from the ACCOUNT (#130):")
    # REPORTED ON #130, FROM A REAL BLOCK IN A CONSUMER'S TREE. A gate selected the work it holds
    # you to with `gh pr list --author "@me"`. Where several agent sessions share one GitHub login
    # — the normal case on this machine — `@me` identifies the ACCOUNT, not the session. The gate
    # saw every session's open PRs as its own and fired on whichever session happened to be ending
    # a turn: session A opened the PR, session B was blocked by it, and the only remedy offered was
    # to touch a marker meaning "I wrote the description". Session B refused, correctly, under
    # turn-end blocking pressure.
    #
    # WHY THIS IS ASSERTED RATHER THAN ONLY WRITTEN DOWN. The rule lives in the examples README,
    # and a rule that lives only in prose is the thing this repo exists to stop being. game_loop
    # cannot enforce it inside a consumer's own triggers.d — but it CAN refuse to ship an example
    # that breaks it, and the examples are what people copy.
    #
    # WIDENING IS FINE FOR A NOTICE and corrosive for a BLOCK, which is why the rule is keyed on
    # blocking rather than on the selector: an issue involving the account is worth knowing about
    # whoever touched it, but an obligation routed to a session that cannot discharge it honestly
    # leaves lying as the only way forward.
    # THE DETECTOR PROVES IT CAN FIRE BEFORE ITS SILENCE ON REAL FILES IS WORTH ANYTHING. This is
    # the encoded form of the only thing that caught either of its two broken versions: build the
    # bad input, watch it fail. A detector weakened later — an anchor narrowed, a strip removed —
    # goes red HERE, naming the shape it stopped seeing, instead of going quiet over the examples.
    for _plabel, _pbody, _want_block, _want_acct in DETECTOR_PROBES:
        _gb, _ga = classify_example(_pbody)
        check("the detector itself is exercised — %s" % _plabel,
              (_gb, _ga) == (_want_block, _want_acct))
    _examples = sorted(f for f in os.listdir(EXAMPLES_DIR) if f.endswith(".sh"))
    check("there are examples to check at all — an empty directory would pass every rule below "
          "while proving nothing about what this repo ships",
          len(_examples) >= 3)
    for _name in _examples:
        _blocks, _account = classify_example(
            open(os.path.join(EXAMPLES_DIR, _name)).read())
        check("%s: %s, and %s" % (
                  _name,
                  "can exit non-zero" if _blocks else "always exits 0",
                  "SELECTS ON THE ACCOUNT" if _account
                  else "does not decide whose work it is from the account"),
              not (_blocks and _account))
    check("...and the rule the examples are held to is WRITTEN DOWN where somebody copying one "
          "will read it, because the assertion above protects this repo's examples and cannot "
          "reach a consumer's own triggers.d",
          "@me` is an ACCOUNT, not a session"
          in open(os.path.join(EXAMPLES_DIR, "README.md")).read())

    print()
    print("meta — every trigger tested above has both a firing case and a quiet case:")
    for name, cov in sorted(COVERAGE.items()):
        check(f"{name}: {'firing+quiet' if cov['fired'] and cov['quiet'] else 'INCOMPLETE'}",
              cov["fired"] and cov["quiet"])

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
