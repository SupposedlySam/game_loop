"""Producer mutation sweep: neuter one silence-on-pass producer at a time and see which
assertions survive.  Run:  python3 test/mutation_sweep.py

A guard's "allow" is silence; so is a detector that finds nothing, a validator with no
complaints, and a nudge that declines to fire. Every "stays quiet" / "gets no nudge" /
"passes untouched" assertion is satisfied by a producer that has stopped working.

Each entry rewrites one function's body to the neutered form, runs the suite, and reports how
many named assertions the mutation KILLED — a set difference against an unmutated baseline run,
not a guess. It also prints the checks that STILL PASS whose text suggests they are about that
producer's silence; that filter is a crude keyword match and pulls in unrelated assertions, so
the KILL COUNTS are the reliable signal and the survivor list is a lead to follow by hand.

A FLOOR WITH A REASON ATTACHED — NOT A SCORE, AND NOT A TARGET. These are what the numbers were
after #42, recorded so a later reader can tell drift from noise without having been there. Calling
them "a baseline to beat" was the original wording here and it was wrong in a way this file is
otherwise about: a kill count is not coverage — ten assertions reading one line of output flip
together and count ten — so a number can be raised without a single behaviour becoming better
protected, and a docstring that invites you to beat it is asking for exactly that.

The honest use is the ORDERING. Ranking is the one thing these numbers genuinely do: strengthen in
ascending kill order, because "which non-events did I assert alone" is a property this sweep
MEASURES, while "which behaviour feels under-tested" is one you would have to guess. That ordering
rule was tested — a hypothesis that recently-argued behaviours are the weakest came back NEGATIVE,
and the real predictor turned out to be simply whether a non-event assertion ever got a companion.

(#42, measured — before → after the companion assertions that issue added):

    producer            before   after
    unpushed_warning       4    →   7
    fix_warning            9    →   9     already paired; nothing was owed
    category_tell          2    →   4
    aggregate_tell         1    →   7
    dominance             10    →  10     not touched by #42
    ruled_out              1    →   1     still THIN; see its entry in MUTANTS

(suite total 431 → 446 over the same change.)

A run that comes back BELOW those numbers is drift, not noise. A run ABOVE them proves nothing on
its own: check that the added assertions are companions in the same observation, and not this
sweep's own metric being farmed. See THIN_AT.

THE RULE THAT WOULD HAVE PREVENTED ALL OF THIS, and it costs nothing at write time: when you assert
that something did NOT happen, pair it in the same observation with the case where it DOES. Two
producers survived their mutation only because someone happened to do that the day they wrote the
test — `fix_warning` here, and a `waiting` producer in a downstream project, different repos, no
shared author on those lines. Two accidents are a better argument for a rule than any reasoning
about it. Retrofitting the pairing is expensive; writing it costs a single extra capture.

The named restraint assertions — "up to date with upstream → checkpoint stays quiet", "a branch
with NO upstream stays quiet", "a claim filed WITH a scope is not also nudged about one", "an
ordinary instance claim is untouched", "a plain observation claim gets no aggregate nudge" — STILL
SURVIVE, and are meant to. They were never false, only unsupported, and each one now sits beside a
companion, in the same observation, that dies. Deleting or rewriting one to clear it from the
survivor list would remove the restraint claim and leave the count looking better.

WHAT THIS DOES NOT CATCH (INV6). It measures whether an assertion NOTICES a producer that has
stopped producing. It says nothing about whether the producer is RIGHT: a wrong message and a
correct one are killed identically, because both are non-empty. It cannot see a producer whose
broken form is not the neutered one written here — a validator that wrongly ACCEPTS, a detector
that fires on everything. And a high count is not coverage: ten assertions against one line of
output kill together and count ten.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = ".game_loop/bin/game_loop"

# The line between "report it" and "fail the run".
#
# UNPROTECTED (zero kills) FAILS. A producer whose absence nothing in the suite notices is the case
# this sweep exists for, and it is not a matter of taste.
#
# THIN is reported and does NOT fail. A producer that reports by silence needs a PAIR per behaviour
# — the message, and the control proving the silence was a verdict — and it has at least two
# behaviours worth naming: it fires, and it declines. Under 3 kills at least one of those is resting
# on nothing. But that is a heuristic about a suite, and some entries are thin for reasons that
# "fixing" would make worse (a test isolating one component from another on purpose). A sweep that
# nags on every acceptable-thin entry is a warning that fires every time: it gets run with its output
# ignored, and then the zero-coverage case it exists for goes unseen too. Thin argues; zero blocks.
THIN_AT = 3

UNPROTECTED, THIN, OK = "UNPROTECTED", "THIN", "ok"


def verdict(killed):
    """How a producer's kill count should be read. Pure, so the suite can check this line itself."""
    if killed == 0:
        return UNPROTECTED
    return THIN if killed < THIN_AT else OK


# (label, function name, neutered body, substrings that mark an assertion as being about it,
#  a note saying WHY this entry is thin — written next to the number, because a thin entry with no
#  reason attached invites the one bad fix: un-isolating a test to move the count, which is exactly
#  the rounding-up this sweep exists to catch. The note must distinguish thin-and-correct from
#  thin-and-unfixed; a known gap says so, and does not get to describe itself as a decision.)
MUTANTS = [
    ("unpushed_warning -> never warns", "unpushed_warning", "    return None\n",
     ["unpushed", "upstream", "quiet"], None, 7),
    ("fix_warning -> never warns", "fix_warning", "    return None\n",
     ["fix", "quiet", "silence"], None, 9),
    ("category_tell -> never detects", "category_tell", "    return None\n",
     ["nudge", "category", "scope"], None, 4),
    ("aggregate_tell -> never detects", "aggregate_tell", "    return None\n",
     ["nudge", "aggregate", "sum"], None, 7),
    ("dominance -> never finds an outlier", "dominance", "    return None\n",
     ["dominan", "distribution", "spread", "event"], None, 10),
    ("ruled_out -> finds no refutations", "ruled_out", "    return []\n",
     ["ruled", "refut"],
     # KNOWN GAP, not a decision. Its survivors all belong to the WRITE side (`--outcome refuted`
     # refusing prose evidence, the log entry) which is separately and well covered; ruled_out() is
     # the READ side, and exactly one assertion — status reprinting the standing list — notices when
     # it returns nothing. #42 scoped itself to the four nudge/warning producers and did not touch
     # this. Fixing it means asserting a later session INHERITS the list, not deleting this note.
     "the read side of the refutation path; #42 scoped itself elsewhere and left it unfixed", 1),
]


def neuter(src, fn, body):
    """Replace fn's body with `body`, keeping its signature and dropping its docstring."""
    lines = src.split("\n")
    for i, l in enumerate(lines):
        if re.match(rf"^def {re.escape(fn)}\(", l):
            j = i + 1
            while j < len(lines) and (lines[j].startswith((" ", "\t")) or not lines[j].strip()):
                j += 1
            return "\n".join(lines[:i + 1] + [body.rstrip("\n")] + lines[j:]), True
    return src, False


def run(tree):
    r = subprocess.run([sys.executable, "test/run.py"], cwd=tree,
                       capture_output=True, text=True, timeout=1800)
    return r.stdout


def passing(out):
    return [m.group(1) for m in re.finditer(r"^  ok   (.*)$", out, re.M)]


def main():
    base = tempfile.mkdtemp(prefix="sweep-base-")
    subprocess.run(f"git -C {REPO} archive HEAD | tar -x -C {base}", shell=True, check=True)
    with open(os.path.join(base, BIN)) as f:
        original = f.read()

    print("producer mutation sweep — assertions that SURVIVE a neutered producer")
    print(f"(the tree under test is HEAD, not the working copy; thin under {THIN_AT} kills, "
          "only ZERO fails)\n")
    # The unmutated run, so a kill is a named assertion that FLIPPED rather than a count that moved.
    baseline = set(passing(run(base)))
    print(f"baseline: {len(baseline)} named assertions pass unmutated\n", flush=True)

    verdicts = []
    for label, fn, body, marks, thin_note, floor in MUTANTS:
        t = tempfile.mkdtemp(prefix="sweep-")
        try:
            mutated, found = neuter(original, fn, body)
            if not found:
                # Not a skip. A producer named here that no longer exists is zero evidence about
                # zero code, and a sweep that shrugs at that is a check that cannot fail.
                print(f"  !! {fn}: NOT FOUND in {BIN} — renamed, or gone. Nothing was swept.\n")
                verdicts.append((fn, None, UNPROTECTED))
                continue
            shutil.copytree(base, t, dirs_exist_ok=True)
            with open(os.path.join(t, BIN), "w") as f:
                f.write(mutated)
            os.chmod(os.path.join(t, BIN), 0o755)
            out = run(t)
            still = set(passing(out))
            killed = len(baseline - still)
            v = verdict(killed)
            verdicts.append((fn, killed, v, floor))
            tail = out.strip().split("\n")[-1]
            drift = "  ↓ BELOW FLOOR" if killed < floor else ""
            print(f"{label}\n  suite: {tail}\n  killed: {killed}   [{v}]"
                  f"   floor {floor}{drift}")
            if thin_note:
                print(f"  why it is thin: {thin_note}")
            for c in sorted(s for s in still if any(m in s.lower() for m in marks)):
                print(f"    SURVIVED: {c[:96]}")
            print(flush=True)   # one full suite per entry: show progress even when redirected
        finally:
            shutil.rmtree(t, ignore_errors=True)
    shutil.rmtree(base, ignore_errors=True)

    thin = [f"{fn} ({k})" for fn, k, v, _ in verdicts if v == THIN]
    bad = [fn for fn, _, v, _ in verdicts if v == UNPROTECTED]
    # A recorded floor that is not checked is prose. THIN is a standing acceptable state, so it
    # reports; DRIFT is never that — it means coverage that existed has been lost, which is the
    # exact regression this tool exists to catch, so it fails. The remedy is not to edit the number
    # quietly: re-record it WITH the reason, the way ruled_out's thinness carries its own.
    drifted = [f"{fn} ({k} < {fl})" for fn, k, v, fl in verdicts
               if k is not None and k < fl]
    if thin:
        print("THIN — reported, not fatal: " + " · ".join(thin))
    if drifted:
        print("BELOW THE RECORDED FLOOR: " + " · ".join(drifted))
        print("Coverage that existed is gone. If the drop is legitimate — assertions consolidated,")
        print("a producer genuinely simplified — re-record the floor WITH the reason. Do not just")
        print("lower the number: an unexplained floor is the target-making this file warns about.")
    if bad:
        print("UNPROTECTED — neutering these killed NOTHING: " + " · ".join(bad))
        print("Nothing in the suite notices when they stop working. That is the failure.")
    if bad or drifted:
        return 1
    print("no producer is unprotected, and none is below its recorded floor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
