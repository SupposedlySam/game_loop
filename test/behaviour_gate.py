"""A change to what game_loop REFUSES must be recorded, or declared as unnoticeable — never neither.

`.game_loop/behaviour.json` is the machine-readable record of what an existing verb now costs or
refuses differently. It ships with the payload, `status` diffs the installed copy against main, and
consumers read the delta instead of re-deriving it from source.

THE MECHANISM WAS BUILT AND THE DISCIPLINE LAPSED SILENTLY. Measured, by a consumer, not by me:
twelve commits touched the refusal paths after the file's first and only entry, and four of them
qualified. The sharpest was a commit target built from a variable, which used to pass SILENTLY and
now denies — so an orchestrator committing through `git -C "$TREE"` for weeks, changing nothing,
gets blocked. That consumer then described the OLD behaviour to every agent it spawns, for days, in
the voice of a measured finding, because re-deriving it from source is what you do when the record
is empty.

The criterion ("would somebody who changed nothing notice?") is a judgement only the author can
make. Nothing checked that the judgement HAPPENED, and an omission is indistinguishable from
"nothing changed" — which is this project's whole subject wearing another hat.

So: default-deny. If a diff adds or removes a REFUSAL LINE, behaviour.json must be touched in the
same change. Both answers are one line and both are on the record; what is refused is silence.

WHAT THIS DOES NOT CHECK, and it is most of it: whether the entry is TRUE, whether it describes the
right change, or whether a refusal changed in a way that does not touch a line matching these
markers. It removes the case where nobody decided.
"""
import re
import subprocess
import sys

# Files whose diffs can change what a consumer is refused. Deliberately not every source file: the
# gate must fire on refusal changes, not on every commit, or it becomes noise and gets disabled.
WATCHED = (".game_loop/bin/guard-writes-impl.sh",
           ".game_loop/bin/guard-mcp-impl.sh",
           ".game_loop/bin/game_loop")
RECORD = ".game_loop/behaviour.json"

# A changed line that grants or withholds permission. `deny "` and `p.error(` are the two verbs that
# actually refuse; BLOCKED/REFUSED catch the message side; ALLOW covers a refusal becoming a pass,
# which is a behaviour change in the other direction and qualifies just as much.
REFUSAL = re.compile(r'(deny\s+"|p\.error\(|\bdie\(|BLOCKED:|REFUSED|print\("DENY"\)|print\("ALLOW"\))')


def changed_lines(ref):
    """Added/removed lines in WATCHED files, against `ref`. Empty when git cannot answer."""
    r = subprocess.run(["git", "diff", "-U0", ref, "--"] + list(WATCHED),
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return [l for l in r.stdout.split("\n")
            if (l.startswith("+") or l.startswith("-")) and not l.startswith(("+++", "---"))]


def record_touched(ref):
    r = subprocess.run(["git", "diff", "--name-only", ref, "--", RECORD],
                       capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def main(ref="HEAD"):
    """The ref is a PARAMETER, not something read out of sys.argv in here.

    Reading argv inside a function means judging whatever command line the process happens to have,
    which is only the caller's when the caller is the shell. Driven from another program this took
    that program's first argument as a GIT REF -- `--section`, say -- and the diff then failed, and
    the failure returned 0. The same shape cost a suite run the same day in mutation_sweep.py.
    """
    lines = changed_lines(ref)
    if lines is None:
        # COULD NOT RUN IS NOT A PASS, and until now it shared an exit code with one. This prints a
        # sentence about having no verdict and then returned 0 -- and `verify` reads the exit code,
        # not the sentence, so a gate that never ran reported as a gate that was satisfied. Every
        # other check in this repo owes three outcomes; this one owed them too and only had two.
        #
        # Demonstrated rather than reasoned about: `behaviour_gate.py no-such-ref` printed "no
        # verdict" and exited 0, which verify renders as a passing rule.
        print(f"behaviour gate: COULD NOT DIFF against {ref} — no verdict, and that is not a pass.",
              file=sys.stderr)
        print("  git could not answer, so nothing was compared. Exiting 2 rather than 0: a check\n"
              "  that did not run must not be indistinguishable from one that ran and was content.\n"
              "  Fix the ref (or the repository) and run it again.", file=sys.stderr)
        return 2
    hits = [l for l in lines if REFUSAL.search(l)]
    if not hits:
        print("behaviour gate: no refusal line changed — nothing owed.")
        return 0
    if record_touched(ref):
        print(f"behaviour gate: {len(hits)} refusal line(s) changed, and {RECORD} was updated.")
        return 0
    print(f"BEHAVIOUR GATE: {len(hits)} refusal line(s) changed and {RECORD} was not touched.\n",
          file=sys.stderr)
    for l in hits[:6]:
        print("    " + l[:110], file=sys.stderr)
    print(f"\nWhat a consumer is refused may have changed, and they read {RECORD} rather than\n"
          "re-deriving it from your source. Do ONE of these — both are one line, and the point is\n"
          "that the judgement is on the record either way:\n\n"
          "  * add an entry: seq, sha, verb, change, why_it_qualifies\n"
          "  * or add one with \"notice\": false and a reason, if nobody who changed nothing would\n"
          "    notice — a decision, rather than an omission that looks identical to one.\n\n"
          "The criterion is in the file's own header: would somebody who changed NOTHING about how\n"
          "they use game_loop notice this?", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "HEAD"))
