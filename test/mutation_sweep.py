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

The four the ENUMERATION added, measured the same way against the HEAD that introduced it:

    producer               kills
    hooks_live_warning       6
    config_paths_report     12
    worktree_report          2    THIN — see its entry
    update_notice            1    THIN — see its entry

Two more the enumeration named came back at ZERO and are NOT listed in MUTANTS — retro_nudge and
legacy_mandate_warning, both measured, both recorded in NOT_SWEPT with the number. They are a
standing debt, not a clean bill: the sweep would exit 1 forever on them and the remedy is an
assertion nobody has written yet, so parking them with the measurement attached is the honest
state. Do not read their absence from MUTANTS as a verdict that they are covered.

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

DEFAULT-DENY, BECAUSE THE LIST WAS A DENYLIST. For its first six entries this file's coverage was
whatever somebody remembered to type into MUTANTS — the shape that `bin/guard-writes-impl.sh`'s
header and #25 both argue against: a denylist defaults to UNPROTECTED and silently misses whatever
nobody listed, an allowlist defaults to PROTECTED. The tool built to find unprotected things was
unprotected in exactly that way, and it showed. Every weakness it found was in a producer somebody
already suspected; `hooks_live_warning`, `config_paths_report`, `worktree_report` and
`update_notice` were each found weak by somebody who had pointed something ELSE at them.

So the sweep now ENUMERATES ITS OWN CANDIDATES. candidates() parses the script and returns every
module-level function that can return a finding OR a nothing — the silence-on-pass shape. Each one
must appear in MUTANTS or in NOT_SWEPT with a reason, and an unaccounted-for candidate FAILS the
run, before the slow part starts, in the same spirit as UNPROTECTED: a producer nobody decided
about is the whole case this file exists for.

NOT_SWEPT IS NOT A WAIVER LIST. Two kinds of entry live in it and they are meant to read
differently: a genuine exclusion (a helper whose "nothing" means git failed and which is swept
through its callers; a formatter whose silence is a configured opt-out, not a verdict) and a KNOWN
GAP — "should be swept, is not yet", said plainly, the shape ruled_out's note already uses.
Sweeping everything is not the goal and would not be an improvement: each entry costs a full suite
run, and a check too slow to run is a check nobody runs. What is not allowed is an exclusion that
is not true.

THAT LAST SENTENCE CAME TRUE ABOUT THIS FILE (#59). At 41 producers against a 730-assertion suite,
serially, the sweep was ~1.8 hours: measured at ~2.5 min each. I started it three times in one day
and finished it none of them, and two of the abandoned runs left a COLLAPSED BASELINE behind, whose
"killed: 0" is indistinguishable from UNPROTECTED. Slow did not make it wrong; it made it unfinished
and then invited a partial result to be read as a verdict, which is worse.

The producers are independent by construction — each gets its own temp tree, its own suite run, and
shares no state — so they now run concurrently (`GAME_LOOP_SWEEP_JOBS`, default half the cores).
Reports are buffered and emitted in MUTANTS order, so the output stays deterministic and a
redirected run still shows progress. Time per producer is printed, so the next person arguing about
this cost has a number instead of an impression.

The two faster answers were both refused: sweeping fewer producers buys speed with the denominator
this file exists to defend (#44), and running a subset of the suite per producer changes what the
number MEANS, since a kill count is a set difference over the whole suite. Doing the same work at
once is the only one of the three that costs nothing.

WHAT THIS DOES NOT CATCH (INV6). It measures whether an assertion NOTICES a producer that has
stopped producing. It says nothing about whether the producer is RIGHT: a wrong message and a
correct one are killed identically, because both are non-empty. It cannot see a producer whose
broken form is not the neutered one written here — a validator that wrongly ACCEPTS, a detector
that fires on everything. And a high count is not coverage: ten assertions against one line of
output kill together and count ten.

AND A LOW COUNT IS NOT WEAK COVERAGE, which is the same caveat in the direction this file forgot
to state — the direction its own headline advice ("strengthen in ascending kill order") acts on.
A mutant is neutered to the producer's NOTHING value, so every assertion covering the NOTHING
answer survives BY CONSTRUCTION. It cannot flip: the mutant returns exactly what that assertion
expects. Only the something-direction assertions can ever kill, so the count measures one half of
a two-direction contract and calls it the whole.

Measured across the clean sweep of 132: ALL TWELVE of the lowest-ranked producers neuter to their
own nothing-answer. Ten do it in the obvious shapes — `None`, `[]`, `""`, `False`, `(False, "")`.
The other two return TRUE, and mean it the same way: `_parses` -> True is "everything compiles",
`guardtest_directions` -> (True, "") is "every fixture is fine". Both are no-finding. Counting by
shape alone would have missed them, which is the small version of the same mistake. `declared_mutant_unparseable` sits near the bottom at 3 with FOUR assertions in THREE
directions; two of them assert the negative answer and can never contribute. So this file ranks
lowest exactly the producers whose negative direction is best tested, which is the discipline the
rest of it argues for.

The ordering is still the most useful thing here. Read it as "which producers have the fewest
SOMETHING-direction assertions", not as "which are least tested", and open the assertions before
adding any. Strengthening one of these by writing more nothing-direction checks raises no number
and protects nothing new.

The DISCOVERY has its own edge, and it is the same kind: it reads the SHAPE of a return, never the
meaning of one. A producer that signals nothing-found with an empty string, a zero, an empty dict
or a sentinel object is not a candidate — and unlike a producer nobody listed, it will not be
missed loudly, it will simply never be enumerated. Default-deny over the shapes named here is
strictly better than a hand list; it is not the same thing as complete.
"""
import ast
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

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

# A FIFTH VERDICT, from lamp-owner (2026-08-23), who found it in their own catalogues and sent it
# before I looked for it here. UNPROTECTED is TWO findings wearing one word: the producer runs and
# nothing asserts it, or nothing runs it at all. A VALUE mutation cannot separate them — both go
# green — and the remedies are opposite: the first wants an assertion, the second wants a test that
# reaches the code. Writing an assertion for a producer nothing calls is the wrong repair, made
# confidently, and this file would have recommended it.
#
# The probe is a `raise` in the body rather than a neutered return, which is the same instrument
# `mutate --prove` uses one level up. Its verdict names BOTH remaining causes rather than choosing:
# a suite that stays green with a producer CRASHING may never call it, or may call it somewhere no
# assertion can see — a subprocess whose stderr nobody reads. Those are still different bugs, and
# this cannot tell them apart, so it does not pretend to.
INERT = "INERT (nothing reacts to it CRASHING)"
_PROBE_MARK = "GAMELOOP-SWEEP-LIVENESS-PROBE"

# NOT MEASURED IS A THIRD OUTCOME, not a low score (showrunner, this week). A mutant whose suite
# CRASHED, timed out, or was never applied has no measurement at all — and every one of those
# produced a NUMBER here, which is the substitution this project exists to refuse, in its own
# instrument. It is reported separately and it FAILS the run: a producer nobody could score is a
# hole in the denominator, which is this file's oldest lesson.
NOT_MEASURED = "NOT MEASURED"


def fast_without_map_notice(fast, section_map):
    """The line a FAST run owes when there is no section map, or "" when it owes none.

    At module level for the reason every rule here is: one that lives inside main() cannot be
    driven by the suite. This one especially — its whole subject is a run that produces correct
    results and says nothing, so an unassertable version of it would be the same defect again.
    """
    if not fast or section_map:
        return ""
    return ("GAME_LOOP_SWEEP_FAST=1 was asked for and there is NO SECTION MAP -- "
            "test/sweep-sections.json\n"
            "  is missing or unreadable, so every producer falls back to the WHOLE suite. The "
            "results are\n"
            "  correct and the speedup is gone; this run takes about as long as a full one. The "
            "map is\n"
            "  written by a FULL sweep, so run one once and fast mode works after that.")


def marks_missing_killers(marks, killer_names):
    """(matched, total) — how many of a producer's ACTUAL killers its mark set names.

    The counterpart to `overbroad_marks`, and the direction that was missing. Breadth asks how many
    assertions a mark set matches; this asks whether it matches the ones that MATTER, which is what
    `killers()` uses it for. The two do not correlate: a set matching one name in the whole suite
    reads as precise and can still match none of the killers.

    At module level for the reason every selector here is: a rule that lives inside main() cannot be
    driven by the suite, and a rule nobody can drive is one that gets believed rather than checked.
    """
    if not marks or not killer_names:
        return 0, 0
    hit = sum(1 for k in killer_names if any(w in k for w in marks))
    return hit, len(killer_names)


def overbroad_marks(marks, names, floor):
    """How many of `names` a mark set matches, and whether that breadth makes it undiscriminating.

    Returns (matched, is_overbroad). Pure, and at module level for the reason every selector here is:
    a rule that lives inside main() cannot be driven by the suite.

    THE MARKS ARE WHAT SEPARATES A GENUINE KILL FROM COLLATERAL — `killers()` asks whether a killed
    assertion's NAME carries one. Measured 2026-08-25 across 1626 assertion names, `read_probe`'s
    marks matched 306 of them against a floor of 3, `note_line`'s 287, `remote_has_ref`'s 279. At
    that breadth the question answers YES for almost any kill, so the check that exists to catch
    collateral agrees with whatever it is shown. The cause was short marks under substring matching —
    "ref" is inside refuse, "note" is in a third of these names — and NOT the matcher: exactly 8
    marks lose every match under a word-boundary rule, and 7 of those are deliberate stems (refut,
    supersed, attach, exhaust, dogfood) that the rule would break to fix an unrelated problem.

    THE BOUND IS A RATIO, not a count: a mark set that matches more than ten times its floor is
    claiming a subject far wider than the coverage it records. Twenty is the floor on the count so a
    producer with a floor of 1 and 11 matches does not fire — that is a small producer, not a broad
    mark.
    """
    matched = sum(1 for n in names if any(m.lower() in n.lower() for m in marks))
    return matched, (matched >= 20 and matched > 10 * max(floor, 1))


def _write_killers():
    """Persist the COMPLETE killer set per producer, because the report renders only three of them.

    The sweep's own advisory tells you to narrow a mark that "matches far more assertion names than
    it records coverage for" — and then gives you nothing to narrow it WITH. `targeted[:3]` is what
    reaches the screen, so the input the advice requires is computed, rendered down to a third of a
    line, and dropped. Deriving new marks from those three is how a set silently loses a real killer
    and the floor tripwire fires on the next run for a reason nobody can reconstruct.

    A rendered report is not a data structure. This writes the sets themselves, so a proposed mark
    can be tested against every name that actually killed the producer instead of against a sample.

    Full sweeps only, and for the same reason the section map is: a trimmed run has not observed
    what it did not execute, and letting it rewrite this would ratchet the sets toward whatever ran
    last.
    """
    if not KILL_NAMES or not _is_full_sweep():
        return
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "sweep-killers.json"), "w") as f:
            json.dump({k: sorted(v) for k, v in KILL_NAMES.items()}, f, indent=1, sort_keys=True)
        sizes = sorted(len(v) for v in KILL_NAMES.values())
        print("wrote sweep-killers.json: %d producers, %d killer name(s) in total, median %d each "
              "— the set a mark must keep matching if it is narrowed"
              % (len(KILL_NAMES), sum(sizes), sizes[len(sizes) // 2]))
    except OSError:
        pass


def _write_section_map(tree):
    """Write {producer: [section, ...]} from the COMPLETE killer sets this run measured.

    `tree` IS READ FROM, NEVER WRITTEN TO, and that asymmetry is worth stating because it is not
    guessable from the signature. The map is written beside THIS MODULE —
    `os.path.dirname(os.path.abspath(__file__))` — while `tree` supplies the `--list-sections`
    output and the run.py source the killer names are located in. It has to be that way: the sweep
    passes a git-archive temp directory, so writing there would throw the map away with the tree.

    Undocumented, it cost the real map. Driving this against a temp tree to test it overwrote this
    repo's own test/sweep-sections.json with a fixture-derived one — 120 producers all pointing at
    a single section, which is the "a wrong map makes producers come back short" case this file
    warns about two paragraphs down. A test fixture must load this module FROM A COPY so `__file__`
    moves with it.

    A byproduct of a full sweep, and only of a full sweep: a trimmed run does not observe the
    sections it did not execute, so letting it rewrite the map would let the map shrink toward
    whatever it happened to run last — a ratchet, tightening on itself until it measures nothing.
    """
    try:
        r = subprocess.run([sys.executable, "test/run.py", "--list-sections"], cwd=tree,
                           capture_output=True, text=True, timeout=120)
        secs = sorted((int(m.group(1)), m.group(2).rstrip())
                      for m in (re.match(r"^\s*(\d+)\s+(.*)$", l) for l in r.stdout.splitlines())
                      if m)
        src = open(os.path.join(tree, "test", "run.py")).read().split("\n")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("sweep: could NOT write the section map (%s: %s) — the next --fast run will fall "
              "back to the whole suite for every producer, which is slow and correct rather than "
              "fast and wrong." % (exc.__class__.__name__, str(exc)[:80]))
        return
    name_line = {}
    for i, line in enumerate(src, 1):
        m = re.search(r'check\("([^"]{6,})', line)
        if m:
            name_line.setdefault(m.group(1)[:60], i)

    def section_of(ln):
        cur = None
        for s_ln, nm in secs:
            if s_ln <= ln:
                cur = nm
            else:
                break
        return cur

    # A KILLER WHOSE SECTION CANNOT BE FOUND DISQUALIFIES THE WHOLE PRODUCER. The name index is
    # built by regex over `check("...` literals, so an assertion whose name is an f-string or is
    # concatenated across lines is not in it. The first version dropped those names and mapped the
    # producer from the rest — and seven producers then came back short by EXACTLY ONE kill each,
    # every one of them a genuine killer living in a section the map had quietly omitted.
    #
    # Dropping an unlocatable name is the same act as a short denominator: the set looks complete
    # because nothing says otherwise. So an unmappable killer means NO ENTRY, which means this
    # producer runs the whole suite — slow and right, rather than fast and one short.
    if not _is_full_sweep():
        return                       # explicit now; this used to be protected only by accident
    out, unmappable = {}, []
    for key, names in KILL_NAMES.items():
        missing = [n for n in names if n[:60] not in name_line]
        if missing:
            unmappable.append((key, len(missing), len(names)))
            continue
        found = {section_of(name_line[n[:60]]) for n in names}
        found.discard(None)
        if len(found) != len({section_of(name_line[n[:60]]) for n in names}):
            continue                      # a name mapped to no section at all: same rule
        if found:
            out[key] = sorted(found)
    if unmappable:
        print("  %d producer(s) keep the WHOLE suite: a killer's section could not be located "
              "(f-string or wrapped assertion name). Not a gap in coverage — a gap in the MAP, and "
              "the safe answer is to run everything: %s"
              % (len(unmappable), ", ".join("%s (%d/%d)" % (k.split("::")[-1], m, t)
                                            for k, m, t in unmappable[:4])))
    if not out:
        return
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "sweep-sections.json"), "w") as f:
            json.dump(out, f, indent=1, sort_keys=True)
        spans = sorted(len(v) for v in out.values())
        print("wrote sweep-sections.json: %d producers, median %d section(s), max %d "
              "(GAME_LOOP_SWEEP_FAST=1 uses it)" % (len(out), spans[len(spans) // 2], spans[-1]))
    except OSError:
        pass


def stale_low_floors(verdicts):
    """[(floor, killed, name)] for producers whose floor would permit losing MORE THAN HALF of what
    they measure today. Pure, and at module level so the suite can drive it — the same lift
    `probed_verdict`, `killers` and `note_line` got, for the same reason.

    The bound is the halving rather than a round number: it states the size of the BLIND SPOT rather
    than an opinion about drift, and it does not fire on the ±1 churn ordinary assertion edits make.
    NOT_MEASURED is excluded because it has no reading to compare against — a producer nobody could
    score is not a producer with a generous floor.
    """
    return sorted((fl, k, fn) for fn, k, v, fl in verdicts
                  if k is not None and v != NOT_MEASURED and fl < k / 2)


def note_line(v, thin_note):
    """The one line that explains a floor — or says that nobody has. Pure, and at module level for
    the same reason `probed_verdict` and `killers` are: a renderer nested in main() cannot be driven
    by the suite, and this one decides whether debt is visible.

    AN UNEXPLAINED LOW FLOOR MUST NOT READ AS AN EXPLAINED ONE. The MUTANTS header says a thin
    number with no reason attached "invites the one bad fix: un-isolating a test to move the count".
    Nothing said so when the note was simply ABSENT: the line vanished, and a floor nobody had ever
    explained rendered identically to one somebody had thought about and accepted. Same shape as the
    verdicts this file keeps apart everywhere else, arriving in its own report. 15 entries were in
    that state when this was written, 11 of them at zero.
    """
    if thin_note:
        return f"  {'why it is thin' if v == THIN else 'what it covers'}: {thin_note}"
    if v in (THIN, UNPROTECTED):
        return (f"  {'why it is thin' if v == THIN else 'why it is unprotected'}: NOT STATED — "
                "nobody has written down why this floor is what it is, so this is undescribed debt "
                "rather than an accepted number.")
    return None


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
# ── FLOORS RAISED 2026-08-27 FROM THE FULL SWEEP OF 3b00196. ──────────────────────────────────
# That run reported 28 floors as STALE-LOW — "measured well above what is recorded, so the tripwire
# has slack" — and said what to do about it in its own words: "Raising a floor is recording a
# MEASUREMENT, not tightening a screw: take the number from a full sweep of the tree it describes."
# So each of those 28 now carries the number that sweep measured. The list came from the per-producer
# readings in the log rather than from the summary line, which truncates at twelve with a bare "…".
#
# The measurements predate the guardtest and #117 work that landed in the same session. That is the
# SAFE direction: those commits only ADD assertions, so a producer's real count can be higher than
# what is recorded here but never lower — a floor is a minimum, and one taken from a smaller suite
# still catches the loss it exists to catch.

MUTANTS = [
    # ── TWO KNOWN GAPS CLOSED, and the arithmetic that nearly let one through. ─────────────────
    # Both sat in NOT_SWEPT as declared debt. Measured by hand against an archived HEAD before
    # moving them here, and the raw numbers were FLATTERING: neutering a producer that is still IN
    # NOT_SWEPT breaks two accounting assertions (`set(NOT_SWEPT) <= set(real_found)`), because the
    # neutered shape stops being discovered as a candidate and the exclusion then names something
    # the tree no longer has. So every declared-gap producer scores 2 free kills that DISAPPEAR the
    # moment it moves into this list.
    #
    #   binding_windows          raw 7 · artefact 2 · genuine 5   (re-measured out of NOT_SWEPT: 5)
    #   legacy_mandate_warning   raw 2 · artefact 2 · genuine 0   <- the NOT_SWEPT note said 0
    #
    # Both re-measured after the move with the new assertions in the tree: binding_windows 5, and
    # legacy_mandate_warning 0 -> 2, the two arms of it that can flip. The subtraction predicted
    # binding_windows exactly, which is the only reason to trust the subtraction at all.
    #
    # The note was right and my subtraction was the only thing standing between "0 kills, still
    # unprotected" and a floor of 2 recorded against an assertion about bookkeeping. Anyone else
    # promoting a gap from that list has the same trap waiting: measure it, then subtract the two.
    # ── AND THE REMAINING FIVE, so the KNOWN GAPS queue is now EMPTY. ────────────────────────────
    # All measured against ONE instrument — the same archived HEAD with the same assertions — after
    # the guards below, because two of them could not be measured at all until then. Floors are the
    # genuine counts: raw minus the 2 accounting kills every NOT_SWEPT member scores for free.
    #
    #   pinned_report     150    parse_events 21    recurrence_lines 8    metric_movement 3
    #   _asked_the_user     3
    #
    # SWEEPING THESE FOUND TWO NEW CRASH SITES, which is the argument for sweeping them. Neutering
    # pinned_report and recurrence_lines ended the run instead of failing an assertion — an
    # unguarded `json.loads(stdout)` and a bare `.index()`. The 81-producer sweep an hour earlier
    # reported ZERO crashes and was right about its own denominator: the crash class was gone from
    # the SWEPT set, not from the file. A gap is not only uncounted coverage, it is uncounted
    # exposure to the class the counting depends on.
    # 150 -> 170 after a crash I introduced was removed. The sweep that followed my own THIN
    # work reported this producer NOT MEASURED: I had written `"\n".join(_gpd.pinned_report())`
    # in a new assertion, and a neutered producer returns None, so join(None) ended the run.
    # Every join over a report producer in the suite is guarded now, not just that one.
    #
    # Worth stating plainly: I spent the day removing this exact class and then added an
    # instance of it, in the assertions written to raise this producer's coverage, and the
    # cost was the producer losing its reading entirely. Fluency is not protection; the sweep
    # is.
    ("pinned_report -> the PINNED CODE block is never reported",
     ".game_loop/bin/_gl_impl.py::pinned_report", "    return None\n",
     ["pinned", "self"], None, 217),
    # THIN AT 1, AND THE THINNESS IS STRUCTURAL — do not "fix" it by adding assertions.
    # `_pin_marker_sha` returns a sha or None, so the neutered body IS its nothing-value: the two
    # assertions covering the absent-marker and corrupt-marker paths survive by construction, not
    # by weakness. They still earn their place (they would catch a raise, or a bare "" reported as
    # agreement between the pin and HEAD) — they simply cannot ever kill THIS mutant. Only the
    # positive read can, and there is exactly one of those to have. Measured 1 against the section
    # that asserts it, at the commit that introduced it; confirm at the next full sweep.
    ("_pin_marker_sha -> a pin's own commit stamp is never read, so `self` cannot tell a "
     "consumer their gates are running older code than the tree they guard",
     ".game_loop/bin/_gl_impl.py::_pin_marker_sha", "    return None\n",
     ["pin", "wiring", "marker"],
     "3 kills, and the ceiling is STRUCTURAL — do not try to raise it. This "
     "returns a sha or None, so the neutered body IS its nothing-answer, and the two assertions "
     "covering the absent-marker and corrupt-marker paths survive by construction. They earn their "
     "place (they would catch a raise, or a bare \"\" reported as agreement between pin and HEAD) "
     "and they can never kill THIS mutant. Only the positive read can, and there is exactly one of "
     "those to have. Adding more nothing-direction assertions raises nothing.", 3),
    ("parse_events -> no per-event distribution is ever seen",
     ".game_loop/bin/_gl_impl.py::parse_events", "    return []\n",
     ["events", "dominance"], None, 21),
    ("recurrence_lines -> a reason that bought the hatch before is never called out",
     ".game_loop/bin/_gl_impl.py::recurrence_lines", "    return []\n",
     ["authorize", "recurrence", "callout", "recurring"], None, 8),
    ("metric_movement -> the metric never appears to have moved",
     ".game_loop/bin/_gl_impl.py::metric_movement", "    return None\n",
     ['the metric', 're-check is', 're-check refusal'], None, 3),
    ("_asked_the_user -> no turn is ever seen to have asked",
     ".game_loop/bin/_gl_impl.py::_asked_the_user", "    return False\n",
     ['half-written final',
      'park does',
      'an oversized',
      'blocks a question'], None, 3),
    ("binding_windows -> no usage window ever binds",
     ".game_loop/bin/_gl_impl.py::binding_windows", "    return []\n",
     # NARROWED FROM ["limit", "handoff", "gate"], which failed in BOTH directions: it matched 166
     # of 1940 assertion names while naming only 3 of this producer's 5 actual killers. Measured
     # against test/sweep-killers.json rather than chosen by eye — these five phrases match all 5
     # killers and 5 names in the whole suite. Broad AND incomplete is the common case, not the
     # exception: 90 of 107 mark sets here miss killers they own.
     ["limit gate", "OWN handoff path", "ordinary work over the threshold",
      "sibling session that wrote nothing", "arms for that editor-hosted"], None, 5),
    # legacy_mandate_warning had NOTHING exercising it — a mandate bound before state went
    # per-session sits in the repo-global file gating nobody, the stop gate and watchdog go quiet,
    # and this producer is the only thing that says so. Four arms written: the warning, both halves
    # of its remedy, the inactive case, and the no-session case.
    #
    # THE FLOOR IS 2, AND I FIRST WROTE 3. Four assertions, but only two can FLIP — the inactive and
    # no-session arms assert an ABSENCE, which a dead producer also produces, so they hold either
    # way. They are worth having (without them the warning could be a line that always prints) and
    # they are not coverage of this producer. Counting assertions written instead of assertions that
    # can flip is how a floor gets set above its own measurement, which this file exists to refuse,
    # and I did it in the same change that quotes the rule. Measured 2, THIN, recorded THIN.
    ("legacy_mandate_warning -> a mandate that gates nobody is never announced",
     ".game_loop/bin/_gl_impl.py::legacy_mandate_warning", "    return None\n",
     ['BOTH halves', 'the recovery', "tell' must", 'a repo-global'], None, 6),
    # ALL THREE MEASURED IN ONE RUN, WITH THE TESTS HELD STILL. The floors recorded for the first
    # two an hour earlier were not wrong about the code — they were measured against an assertion
    # set I then rewrote, so the next sweep read them as coverage disappearing. A floor is only
    # comparable to one taken with the same instrument, which is why these three go together and
    # why nothing is recorded from a run whose assertions moved.
    #
    # MEASURED: 3 · 1 · 2. The first two of those replace a 3 and a 4 taken against the OLDER
    # assertion set — same code, different instrument, and the 3/4 were never a claim about this
    # tree. Two land THIN, which is reported and not fatal, and is the finding rather than an
    # embarrassment: eight assertions cover these gates and only one or two FLIP when the producer
    # is neutered, because most of them exercise the gate through the stop hook rather than through
    # the producer's own verdict. Recorded at what was measured, with the thinness visible, because
    # a floor written above its measurement is the target-making this file exists to refuse.
        # CHASED, NOT LOWERED. This came back 1 < 3 and the drop was REAL: neutered with this file's
    # own mutator against the archived tree, retro_nudge changed nothing observable at all. Three
    # instrument explanations were ruled out by measurement first — duplicate assertion names (the
    # suite has exactly one), the 7 archived-tree failures that cannot flip (all pin/central-install,
    # none retro), and my own hand-rolled neutering, which broke the file and produced 58 failures
    # across unrelated subsystems. That last one was not a result; it is why the real run used
    # neuter().
    #
    # So the floor was measured against an assertion set that had stopped covering it, and this
    # producer is the LINE THAT TELLS AN AGENT A RETRO IS DUE — with the reported symptom from a
    # human running many agents being exactly "they ignore when a retro is due and never check
    # again". Six assertions now cover both arms and both counters, and `mutate --prove` confirms the
    # suite goes RED when it stops nudging. The floor STAYS 3 until the next full sweep re-measures
    # it with the same instrument; raising it from the run that prompted the fix would be a number
    # from a different experiment.
("retro_nudge -> never says a retro is due",
     ".game_loop/bin/_gl_impl.py::retro_nudge", "    return None\n", ['TRANSITIONS alone', 'say DIFFERENT', 'EVIDENCE WORK', 'a lowered', 'that LOGS'], None, 4),
    # THE DEBT THE LAST SWEEP COULD NOT PAY. Both gate turn-end. They sat in NOT_SWEPT for one run
    # — which is what excludes a producer from the instrument — and were ALSO missing from the
    # KNOWN GAPS report because that list matches a prose prefix theirs did not have. Swept now,
    # with floors MEASURED — 3 and 4, from a 28-minute run of all 47 producers, not guessed at.
    # The debt is why they are here: an entry in NOT_SWEPT is invisible to the instrument by
    # construction, so the only way to pay "floor owed on the next sweep" was to stop excluding them.
    ("retro_overdue -> the nudge never escalates",
     ".game_loop/bin/_gl_impl.py::retro_overdue", "    return None\n",
     # THIN AT 1 -> 4. Two assertions drove it and one of them asserted exit 0 at the NUDGE
     # threshold, which a dead gate also produces. The refusal now has to carry the COUNT and the
     # THRESHOLD it crossed (exit 2 plus the word "overdue" is satisfied by any gate closing for
     # any reason), the block has to be COUNTED, and the TRANSITIONS arm is driven at all — only
     # the evidence-work counter was ever exercised, so a producer that had lost the transitions
     # branch entirely passed everything. The refusal must name ITS OWN counter: one that named the
     # wrong debt would send the agent to fix something that is not owed.
     ['at twice', 'that closes', 'it crossed', 'while twice'], None, 4),
    # THIN AT 2 -> 3. The word AFTER is the whole mechanism — the debt is satisfied by a harden
    # logged AFTER the stepback — and nothing exercised it: every sequence in the suite hardened
    # after, so a comparison degenerated into "is there any harden in the log at all" passed them
    # all. That failure would have been permanent and silent, because the log is append-only and
    # shared: ONE harden ever recorded would pay every future retro debt for the life of the
    # checkout, and it would hit hardest the sessions that had been encoding things longest.
    ("retro_debt_open -> the retro never owes its encoding",
     ".game_loop/bin/_gl_impl.py::retro_debt_open", "    return None\n",
     ['no --show', 'harden logged', 'RETRO with', 'ways out'], None, 3),
    # THE CLAIMS EXERCISE. Both floors were MEASURED, and the first measurement of both was 0 —
    # against a tree copied without .git, which cost 166 baseline assertions including the ones
    # being measured, so nothing could FLIP. The control reproduced its floor anyway, because its
    # assertions were not among the missing: a positive control certifies the instrument for THAT
    # case, not globally. Re-measured honestly they were 1 and 2 — thin, and the thinness was the
    # finding: every assertion wrote the observation file by hand, so the RECORDER that runs in
    # production was covered by nothing. Driving it end to end took them to 3 and 5.
    ("_rate_limit_keys -> finds no rate-limit key anywhere",
     ".game_loop/bin/_gl_impl.py::_rate_limit_keys", "    return []\n",
     ['status then', 'the RECORDER', 'a rate-limit'], None, 3),
    ("_hooks_claim_live -> never reports an observation",
     ".game_loop/bin/_gl_impl.py::_hooks_claim_live", "    return None\n",
     ['carry rate-limit', 'dict leaves', 'payload carrying', 'status then', 'ordinary payload'], None, 5),
    ("unpushed_warning -> never warns", ".game_loop/bin/_gl_impl.py::unpushed_warning", "    return None\n",
     ["unpushed", "upstream", "quiet"], None, 7),
    # NARROWED AGAINST THE COMPLETE KILLER SET (sweep-killers.json), not against the three the
    # report renders. "fix" alone matched 103 assertion names against a floor of 9, so `killers()`
    # answered yes for almost any kill and the genuine-versus-collateral distinction stopped
    # discriminating. All nine killers name a fix or a proof, so the subject is nameable: these six
    # phrases keep 9 of 9 and take breadth to 19. ("proved fix" covers "an unproved fix" as a
    # substring, which is why five phrases carry six cases.)
    ("fix_warning -> never warns", ".game_loop/bin/_gl_impl.py::fix_warning", "    return None\n",
     ["fix proof", "fix reported", "proved fix", "no fix", "fix warning", "that fix"], None, 9),
    ("category_tell -> never detects", ".game_loop/bin/_gl_impl.py::category_tell", "    return None\n",
     ['the detector', 'identical sentence', 'a category-shaped', 'nudge names'], None, 4),
    ("aggregate_tell -> never detects", ".game_loop/bin/_gl_impl.py::aggregate_tell", "    return None\n",
     ["nudge", "aggregate", "sum"], None, 7),
    ("dominance -> never finds an outlier", ".game_loop/bin/_gl_impl.py::dominance", "    return None\n",
     ["dominan", "distribution", "spread", "event"], None, 10),
    ("ruled_out -> finds no refutations", ".game_loop/bin/_gl_impl.py::ruled_out", "    return []\n",
     ["ruled", "refut"],
     # 1 -> 7. This note ended "Fixing it means asserting a later session INHERITS the list, not
     # deleting this note" — the remedy named exactly, and it is the FOURTH note in this file to
     # have done that and waited. I nearly closed this one having raised the floor without doing
     # the thing it asked for: count, order, cap and tolerance are all real properties, and none of
     # them is inheritance. Every one of those assertions ran under a single session id, so a
     # version that had quietly become per-session would have passed all of them — which is the
     # whole reason this reads the SHARED log, since the run that must not re-walk a dead path is
     # a LATER session holding none of the state that recorded it.
     "count, newest-first order, the five-item cap, a half-written line skipped, and a DIFFERENT "
     "session inheriting the standing list", 7),
    # The four the hand-written list had never been pointed at. Each was found weak by somebody who
    # was looking at something else, which is the denylist argument stated as history rather than as
    # a principle — they are here because the enumeration named them, not because anyone suspected
    # them. Their floors were MEASURED against this HEAD, not chosen.
    # PAID OFF, not re-declared. This sat in NOT_SWEPT as a KNOWN GAP measured at 0 kills against a
    # 534 baseline: nothing noticed if a superseded watchdog stopped standing down, so two watchdogs
    # could wake one session and no test anywhere would say so. The note recorded the remedy as "an
    # assertion nobody has written yet"; that assertion now exists, and this is it moving into the
    # swept set with a MEASURED floor.
    #
    # THE MEASUREMENT WAS 4 AND THE FLOOR IS 2, DELIBERATELY. Two of the four kills are this file's
    # own accounting reacting to the mutation rather than coverage of the producer: `return False`
    # alone is not the nothing-OR-something shape, so the neutered function stops being a candidate,
    # the NOT_SWEPT/MUTANTS bookkeeping sees a name that no longer exists, and two structural
    # assertions die for reasons that have nothing to do with watchdogs. Recording 4 would book that
    # artifact as protection and make a later honest run look like drift. The two real ones are the
    # paired arms below.
    ("superseded -> never reports supersession", ".game_loop/bin/watchdog::superseded",
     "    return False\n", ['was superseded', 'watchdog whose', 'producer actually'],
     "re-measured today: 2 kills, and BOTH are behavioural — the stand-down itself and the line that SAYS it stood down. This note used to read 'measured at 4, floored at 2: the other two kills are the sweep's own bookkeeping noticing that a neutered superseded no longer parses as a producer'. That reading no longer reproduces, and the accounting assertions changed under it during this session's work. The floor was right; its EXPLANATION had gone stale, which matters because a reader subtracting two artefacts from a future reading of 4 would record a floor of 2 for a producer that had genuinely improved", 2),
    # Measured on the working tree, not on HEAD: claims.json did not exist in HEAD when this was
    # measured, so the archived fixture could not be built and the baseline came back 0 — which the
    # measuring harness now refuses to report a number from rather than publishing an UNPROTECTED
    # that means "the suite died".
    # FLOOR RE-RECORDED 2026-08-24, WITH THE REASON, from a full sweep with the tests held still:
    # measured 25 kills against a floor of 2 that was set when this producer had two arms. It has
    # since gained the three-way version-comparison block and nothing re-reads a floor.
    #
    # A FLOOR IS A FLOOR ON THE TOTAL — the same thing every other floor in this file means. My
    # first version of this line set it to the TARGETED count and said so, which would have put two
    # different metrics in one field and silently redefined every floor recorded before it. 13 is a
    # conservative total: comfortably above the 2 it replaces, and far enough below 25 that the
    # collateral kills drifting (the glyph tripwire and orphan scan redden for any producer at all)
    # cannot fail this entry for a reason that is not a loss of coverage.
    ("external_claims_report -> reports nothing", ".game_loop/bin/_gl_impl.py::external_claims_report",
     "    return []\n", ["claim", "host", "stale"],
     "the three version arms — checked-against-what-is-running, checked-against-something-else, and "
     "nothing-to-compare — plus that status says outright it never checks whether a claim is TRUE", 34),
    ("hooks_live_warning -> never warns", ".game_loop/bin/_gl_impl.py::hooks_live_warning", "    return None\n",
     ["hook", "probe", "live", "wired"], None, 14),
    ("config_paths_report -> reports no keyed path", ".game_loop/bin/_gl_impl.py::config_paths_report", "    return []\n",
     ["config path", "tracked", "write root", "tilde", "read_roots"], None, 12),
    ("worktree_report -> prints no worktree block", ".game_loop/bin/_gl_impl.py::worktree_report", "    return []\n",
     ['opens with', 'the known', 'note rather', 'rules MATCH', 'can emit', 'the drifted'],
     # KNOWN GAP. #30's coverage went almost entirely to `worktree --porcelain`, which reads
     # worktree_drift() — a DIFFERENT producer — so the STATUS block this renders is asserted twice
     # and the rest of it by nothing: "✓ RULES MATCH", the no-parent-harness warning, the UNREADABLE
     # line and the "NOT compared" reach statement can all vanish unnoticed. Fixing it means
     # asserting the matched and unreadable arms of the block, not deleting this note.
     "coverage went to `worktree --porcelain` (a different producer); the status block has two", 8),
    ("update_notice -> never announces an update", ".game_loop/bin/_gl_impl.py::update_notice", "    return None\n",
     ["update", "newer", "sha", "version"],
     # Thin, and defensibly so — but say which. Its two silences ("update_check:false silences the
     # notice", "no VERSION → silent") are real restraint assertions and they DO have a companion
     # that dies. What is thin is the other side: one assertion carries the entire message, so its
     # content — both shas, the re-install command — rests on a single string match.
     "its silences are properly paired; the MESSAGE rests on one assertion, and that is the gap", 26),
    ("limits_inert_warning -> never announces that the limit gates are inert",
     ".game_loop/bin/_gl_impl.py::limits_inert_warning", "    return None\n",
     ["limit", "inert", "snapshot", "tap"],
     # Measured at 6 against a baseline of 480. The paired negative — "a real window silences the
     # warning entirely" — correctly does NOT flip here: neutering to `return None` makes the
     # producer silent everywhere, which is the arm that assertion already permits. That is the
     # pairing behaving as designed rather than a hole, and it is why the count is 6 and not 7.
     None, 6),
    ("fire_triggers -> a project's attachments never run", ".game_loop/bin/_gl_impl.py::fire_triggers", "    return []\n",
     ["trigger", "attach", "harden", "stepback"], None, 31),
    # THE CONTEXT TRIGGER'S TWO HALVES — the one that RECORDS the reading at turn-end, and the one
    # that decides whether it binds. Both MEASURED on the working tree rather than on HEAD, for the
    # reason external_claims_report and fire_triggers above record: the code did not exist in HEAD
    # when this was taken, so an archived baseline could not hold the assertions that flip.
    #
    # Neither count contains bookkeeping noise. `return None` leaves both out of the candidate set,
    # but nothing in this file is keyed on their NAMES the way NOT_SWEPT staleness is, so unlike
    # `superseded` there is no artifact to subtract: all 9 and all 5 are assertions written for the
    # trigger. The recorder scores higher because it also owns the readings the gate never sees —
    # the sidechain skip, the crossing carried forward, the drop back under the cap.
    ("record_context_reading -> no turn-end ever records a context reading",
     ".game_loop/bin/_gl_impl.py::record_context_reading", "    return None\n",
     ["context", "cap", "crossing", "sidechain", "successor verb"], None, 9),
    ("binding_context -> a recorded reading never binds the gate",
     ".game_loop/bin/_gl_impl.py::binding_context", "    return None\n",
     ['handoff that', 'context cap', 'removing the', 'context deny', 'trigger on'], None, 5),
    # THE MOMENT THAT DECIDES (#64), and the most expensive silence in this file: neutered, no
    # attachment can ever refuse a turn-end, every `stop` trigger in every tree becomes advisory,
    # and nothing about a run looks different — a gate that has stopped gating is exactly what this
    # sweep exists to notice. 22 against a baseline of 788, MEASURED on the working tree rather than
    # on HEAD (the producer did not exist in HEAD when this was measured, which is the same reason
    # external_claims_report's floor above was taken that way), by the same neuter/diff this file
    # performs. Every one of the 22 is an assertion written for this moment and none is bookkeeping
    # noticing a changed candidate set.
    #
    # THE FIRST MEASUREMENT READ 59 AND WAS THROWN AWAY. A test read the payload file the attachment
    # writes with a bare `open`, so the neutered producer took the whole suite down at that line and
    # 37 assertions about pinned harnesses "died" for having never run. That is the collapsed
    # baseline this file's header warns about, arriving from the other end, and it inflates rather
    # than zeroes — which is worse, because it looks like coverage. The test now degrades instead of
    # raising, and the number below is from the re-run.
    ("stop_trigger_block -> no attachment can ever refuse a turn-end",
     ".game_loop/bin/_gl_impl.py::stop_trigger_block", "    return None\n",
     ["stop", "turn-end", "attachment", "stood down"], None, 22),
    ("triggers_report -> status never mentions an attachment", ".game_loop/bin/_gl_impl.py::triggers_report",
     "    return []\n", ["trigger", "attach", "never fired"],
     # Measured at 2 when first written, and that was the whole warning: the two assertions were the
     # never-fired pair, so the THIRD state — fires every time and fails every time — was invisible,
     # which is the one a never-fired warning is silent about. Adding it took this to 4.
     None, 26),
    ("retro_outcome -> a retro never reports what the last one yielded", ".game_loop/bin/_gl_impl.py::retro_outcome",
     "    return []\n", ["retro", "yield", "harden"],
     # Also 2 at first, both about hardens. A chapter can be all evidence and no encoding, and the
     # ledger has to show that SHAPE rather than one number, so the claims/triggers arm was added.
     None, 9),
    ("working_tree_report -> never says you are in a different tree", ".game_loop/bin/_gl_impl.py::working_tree_report",
     "    return []\n", ['names each', 'different REPOSITORY', 'check FINDS', 'naming BOTH', 'no shipped', 'worktree it'], None, 8),
    # THE TWO #44 SURFACED THAT MATTER, now measured rather than described. Both were entirely
    # OUTSIDE the denominator until the file list came from git — not excluded, absent.
    ("verify.owed -> nothing ever owes a check", ".game_loop/bin/verify::owed",
     "    return None\n", ["verify", "owes", "stale", "commit"],
     # 23 against a baseline of 534, the highest in this file, and it should be: an always-empty
     # return means `verify --check` reports clean and the commit gate passes EVERYTHING -- #25's
     # failure verbatim, in the function that decides it.
     None, 50),
    ("watchdog.exhausted_windows -> no usage window is ever exhausted",
     ".game_loop/bin/watchdog::exhausted_windows", "    return None\n",
     ['producers this', 'exhausted window', 'park and', 'the resume', 'the usage-limit'],
     # 4. Neutered, the run never parks and never rings itself awake at the reset -- which is
     # exactly the live state #45 found, so the mutation and the real defect are the same thing.
     None, 4),
    # The rest of #44's ten, measured against a baseline of 534 once the file list came from git.
    # Eight of the ten turned out to be genuinely well protected; the surprise was how well, which
    # is worth saying because the issue's framing (and mine) assumed the opposite.
    # 0 -> 81, and it was NOT MEASURED until a fix this morning was finished properly. The
    # coverage block read `cov = json.loads(..)` inside `except ValueError: cov = {}`. When I
    # converted this file's json.loads calls to the non-raising json_text(), that handler
    # became UNREACHABLE — json_text returns None rather than raising — so None flowed into
    # cov.get() and the run died under this mutant.
    #
    # A guard that stops something raising also disables every handler downstream that was
    # catching it. Removing an exception is an interface change, not a local safety fix, and
    # the conversion that was meant to end crash-class readings created one.
    ("verify.changed_files -> no file ever looks changed", ".game_loop/bin/verify::changed_files",
     "    return None\n", ["verify", "changed", "stale", "owes"], None, 81),
    ("verify.staged_files -> nothing is ever staged", ".game_loop/bin/verify::staged_files",
     "    return []\n", ["--staged", "the INDEX", "index scope"],
     "2 kills, and the floor came DOWN from 5 with a reason rather than being quietly lowered. It "
     "was measured UNPROTECTED — zero kills — by the sweep of 2026-08-26, having been probed and "
     "found live: reachable code the suite had stopped driving, which is the tell that separates "
     "'no assertion' from 'no test reaches it'. The 5 was a reading against an assertion set that "
     "no longer exists, so it was not comparable to anything; the sweep says as much in its own "
     "below-floor advice. These are new assertions, driving `verify --coverage --staged` against a "
     "real index, and 2 is what they measure. Only two of the five CAN kill it: the other three "
     "assert what the TREE scope reports and what the report LABELS itself, neither of which this "
     "producer decides.", 2),
    ("notify.send -> a page is never actually sent", ".game_loop/bin/notify.py::send",
     "    return None\n", ["notify", "slack", "page", "send"], None, 11),
    ("watchdog.claim_pidfile -> the watchdog can never claim the pidfile",
     ".game_loop/bin/watchdog::claim_pidfile", "    return False\n",
     ["watchdog", "pidfile", "quiet", "ring"], None, 24),
    ("watchdog.limits_snapshot -> the watchdog never sees a usage snapshot",
     ".game_loop/bin/watchdog::limits_snapshot", "    return None\n",
     ['exhausted window',
      'park and',
      'the resume',
      'the usage-limit'], None, 4),
    ("watchdog.transcript_size -> idleness becomes unmeasurable",
     ".game_loop/bin/watchdog::transcript_size", "    return None\n",
     ['SAME idle',
      'same idle',
      'NEW mandate',
      'engine a',
      'watchdog rings'],
     # THIN at 1, and the shape is familiar: one assertion carries the whole producer. Neutered, the
     # watchdog cannot tell a parked run from a working one -- which is the entire premise of the
     # autonomy engine -- and exactly one named assertion notices.
     "one assertion carries it; the watchdog's own idleness measurement deserves a companion", 7),
    # #37's two, measured at 2 each against a baseline of 543.
    ("behaviour_changes -> the update notice never has anything to report",
     ".game_loop/bin/_gl_impl.py::behaviour_changes", "    return []\n",
     ['usable seq', 'the changes', 'from before', 'update names'],
     # THIN at 2 -> 4. This note used to end "A companion asserting seq order on a scrambled record
     # is what is owed" — the remedy, named exactly, sitting here across several sweeps until
     # somebody wrote it. That is the third note in this file today that diagnosed its own producer
     # and waited. A diagnosis in a comment is a thing to remember, and this repo's first invariant
     # is that a rule the agent has to remember is followed only some of the time; the notes are not
     # exempt from it just because they live next to the measurement.
     #
     # Written now, and the second one was not in the note: a record served SHUFFLED comes back in
     # seq order (seq is the only ordering there is — shas have none — so a reader following the
     # list would otherwise be told what changed in an order that never happened), and an entry with
     # no usable seq is DROPPED while the valid ones still arrive. Tolerant is not blind: one
     # malformed row is neither a reason to report nothing nor a change with no place in the order.
     "seq order on a scrambled record, and one bad row dropped without taking the good ones",
     4),
    ("_remote_behaviour -> the record on main can never be fetched",
     ".game_loop/bin/_gl_impl.py::_remote_behaviour", "    return None\n",
     ['usable seq', 'the changes', 'fetched from', 'from before', 'update names'],
     # 2 -> 5, and this note USED TO SAY "there is little surface to assert beyond that, which is
     # the point rather than a gap". Measurement says otherwise, and the sentence is worth keeping
     # visible because it is the most confident wrong thing in this file: a claim that a producer is
     # thin BY DESIGN reads as a decision already taken, so nobody re-opens it. It was not a
     # decision, it was an absence of looking.
     #
     # The surface it missed is the ARGUMENTS. This takes (repo, raw_base) and builds a URL from
     # both, and the fake server answers ANY path containing "behaviour.json" — so a fetcher that
     # dropped the repo, or read a branch other than main, or looked elsewhere in the tree, passed
     # every assertion here. The request path is now asserted whole. The rest came free: the record
     # it returns must survive being served SHUFFLED and PARTLY MALFORMED, which are properties of
     # what it fetches rather than of the fetch, and were unasserted for the same reason.
     "the request path built from both arguments, plus the shuffled and malformed records it "
     "must carry back intact",
     5),
    ("_compare_versions -> the update check can never tell ahead from behind",
     ".game_loop/bin/_gl_impl.py::_compare_versions", "    return None\n",
     ["update", "ahead", "behind", "ancestry", "determined"],
     # 7 against a baseline of 560. Neutered, every comparison degrades to "could not determine",
     # which is the honest fallback -- so what dies is the ability to tell the three answers apart,
     # which is exactly what #49 was about.
     None, 19),
    ("notify.replies -> the human's answer never arrives", ".game_loop/bin/notify.py::replies",
     "    return []\n", ['human thread', 'a top-level', 'an unmandated', 'every executable', 'the forwarded'],
     # 4, and it took a fix to get a number at all. Neutered, this used to HANG the suite rather
     # than fail it -- the reply poll is a `while True:` whose exits all wait on a reply arriving,
     # so a producer that never produces spun forever and no assertion finished. Bounding the
     # SUITE's watchdog runs (never the product loop, where waiting on the human is correct) turned
     # an un-measurable producer into a measured one. See #50.
     None, 4),
    ("_hooks_stale_warning -> a dead Stop hook is never noticed",
     ".game_loop/bin/_gl_impl.py::_hooks_stale_warning", "    return None\n",
     ['says both',
      'fired then',
      'probe far'],
     # 3 against a baseline of 568. Neutered, a Stop hook that fired once and then died reads
     # exactly like a healthy one for the rest of the session -- which is the defect #43 named:
     # registered, fired, and listening now are three different claims.
     None, 3),
    ("shared_pins -> the checkout's pins are invisible to every session",
     ".game_loop/bin/_gl_impl.py::shared_pins", "    return []\n",
     ["pin", "tidy", "environment", "load-bearing"],
     # 9 against a baseline of 572. Neutered, no pin reaches any status -- which is the state #18
     # shipped in all but name, since a pin only the registering session could see never reached
     # the run that tidies it away.
     None, 9),
    ("waiting_report -> status never says what the run is blocked on",
     ".game_loop/bin/_gl_impl.py::waiting_report", "    return []\n",
     ["waiting", "blocked", "dispatched", "probe"],
     # 1 when first written -- one assertion carried the whole producer -- and 3 after the other
     # three arms were added: NOT WAITING, configured-but-never-run, and the silent no-probe case.
     # Strengthened rather than recorded thin, which is what the ordering in this file is FOR.
     None, 9),
    ("code_files -> two trees always look like they run the same code",
     ".game_loop/bin/_gl_impl.py::code_files", "    return []\n",
     [
       'now actually',
       'harness script',
       "so 'could",
       'it flags',
       'code comparison',
       'drifted harness'],
     # 3 against a baseline of 587. Neutered, the code comparison is empty on both sides, so every
     # pair of trees reports matching harnesses -- which is the state #38 found shipped, where the
     # field was named `harness` and contained only rules.
     None, 8),
    ("session_start_warning -> a harness that never announces itself is never noticed",
     ".game_loop/bin/_gl_impl.py::session_start_warning", "    return []\n",
     ['since hooks', 'it keeps', 'start ever'],
     # THIN at 2, and honestly so rather than padded. The producer emits ONE message, and three of
     # its five assertions are ABSENCE arms -- recorded-and-quiet, opted-out-and-quiet -- which
     # correctly survive a producer neutered to permanent silence. Only the presence arm and its
     # content checks die. Adding more assertions against the same string would raise the number
     # without protecting a single extra behaviour, which is the farming this file warns about.
     # 2 -> 3, and the third arm was ALREADY WRITTEN — it just could not flip. It asserted the
     # caveat "tracks the PROBE's lifetime, not the hook's", and the Stop-probe warning (#43)
     # carries a near-identical sentence; both fire in that fixture, so the substring was satisfied
     # with this producer dead. Two producers sharing a sentence make an assertion on that sentence
     # unable to say which one spoke. Scoped to the text AFTER "NO SESSION START RECORDED" and
     # pinned to the wording only this one uses, it flips.
     "one message plus a caveat scoped to its own block; the rest are absence arms", 3),
    ("refresh_handoff -> no handoff is ever maintained", ".game_loop/bin/_gl_impl.py::refresh_handoff",
     "    return False\n", ['last reported', 'handoff that', 'turn-end leaves'],
     # THIN at 2, and the first measurement said 164 — which was a CRASH CASCADE, not coverage. The
     # tests read the handoff unguarded, so a neutered producer made them raise, the run aborted,
     # and every later assertion counted as killed. A test must survive the thing it tests being
     # ABSENT, because that is exactly what this sweep does to it; an inflated number is worse than
     # a small one, since it reads as protection nobody has.
     "two arms: the file appears, and a hand-written one survives. Its CONTENT is built elsewhere",
     5),
    ("trailing_usage -> the window's consumption is never recorded",
     ".game_loop/bin/_gl_impl.py::trailing_usage", "    return None\n",
     ["usage", "window", "evidence", "consumption"],
     # Also 2, and also honest: the producer feeds one log line, and both assertions read it. It
     # gates nothing by design, so there is little else to assert about it yet -- which is the
     # point, not a gap.
     "one log line, read by both arms; it deliberately gates nothing yet", 20),
    ("pinned_sha -> stable can never be evidenced", ".game_loop/bin/_gl_impl.py::pinned_sha",
     "    return None\n", ['and stable', 'producer actually', 'harness actually'],
     # THIN at 2 by construction, and honestly so: this producer feeds exactly one decision -- can
     # `confidence --mark stable` prove the owning agent is running on this commit -- and that
     # decision has two arms, refused-without-pin and allowed-with. There is no third thing to
     # assert about it, which is the shape of the check rather than a gap in it.
     "one decision, two arms: stable refused without a pin, permitted with one", 2),
    ("installed_confidence_report -> an install never says what level it came from",
     ".game_loop/bin/_gl_impl.py::installed_confidence_report", "    return []\n",
     ['BETA install', 'a PRESENT', 'what stable', 'author DOES', 'an UNRECOGNISED', 'ALPHA commit'],
     # 2 -> 6, AND THE THINNESS WAS HIDING A LIVE DEFECT. Two speaking arms (the alpha warning and
     # the marked-level line) plus an absence arm, and the levels nobody had written an arm for
     # were the ones that were wrong: every value this build did not recognise fell through to the
     # STABLE wording. A typo, a level added in a later version, or a half-written file printed
     # "the author's own agent was running on it" — the strongest claim the scheme can make. An
     # empty CONFIDENCE rendered as "installed from a  commit — the author's own agent was running
     # on it".
     #
     # That is absence reading as reassurance, inside the one producer whose docstring says the
     # whole scheme fails if absence reads as reassurance. Unrecognised and empty are now their own
     # outcomes, and BETA — a weaker claim than stable — is exercised for the first time.
     "alpha, beta, unrecognised and present-but-empty each say something different; absent is "
     "silent and paired", 6),
    ("notify.cfg_source -> status never says WHICH notify.json is paging",
     ".game_loop/bin/notify.py::cfg_source", "    return None\n",
     ['actual PATH', 'a user-level', 'PROJECT file', 'producer behind', 'every executable'],
     # THIN at 1, and honestly so rather than padded: this producer feeds exactly one line of
     # status, and its companions assert the OTHER arm -- that a project-level config stops citing
     # the user file -- which survives a producer neutered to silence, by design. Reporting-only:
     # it decides nothing, which is why there is little to kill.
     # 1 -> 4. The status line was the only consumer and its other cases are ABSENCES — project
     # file wins so no citation is printed, neither file so none either — which a dead producer
     # passes. Now the line must name the actual PATH (not just the phrase "user-level"; somewhere
     # user-level still leaves you opening files to find the one that pages), and the producer is
     # asked directly which file it WOULD use in both arms, so the citation is a fact rather than a
     # fixed sentence.
     "the actual path in the status line, and the producer asked directly in both arms", 4),
    ("config_local_keys -> a local config override is never announced",
     ".game_loop/bin/_gl_impl.py::config_local_keys", "    return []\n",
     ['reports HOW', 'the overridden', 'the keys'],
     # Measured at 0 when first written -- I shipped config.local.json with no test at all, and this
     # sweep is what said so. Then THIN at 1, with this note already naming the reason: the
     # companions assert the OTHER arm (no local file, nothing announced), which survives silence
     # by design. The note was right and sat here unacted-on, which is what a diagnosis in a comment
     # does. 3 now, by asserting what a dead producer cannot fake -- the COUNT of overridden keys
     # and EVERY name in it, in a stable order, over three overrides instead of one. A single
     # override cannot tell a report that lists them from one printing a fixed sentence.
     "the count and every key, over three overrides; the no-override arm is a paired control", 3),
    # The neutered body is `return {}` and not `return []`, deliberately: this is an ACCUMULATOR
    # returning a dict, so an empty LIST makes every .get() on the result raise and the suite dies
    # instead of the behaviour being measured. Given the wrong empty form these read 243 and 609 --
    # crash cascades, not coverage. A mutation has to be the producer's own silence.
    # TWO PRODUCERS SHARE THIS NAME — watchdog::_merged_config and notify.py::_merged_config —
    # and the THIN report names the notify one. I measured the watchdog one first by reading
    # the bare function name off the report instead of the full key, which is the same class
    # as measuring the wrong neuter body: the entry is the fact, not the label on it.
    #
    # Worth having done anyway. This floor was recorded 0 and measures 20: the watchdog config
    # drives the ring cap, the settle window and the waiting probe, so most of that 20 is
    # DOWNSTREAM coverage rather than assertions about the merge. The two that are about the
    # merge are new: it REPLACES a nested block whole rather than merging into it (surprising
    # enough to pin — setting one watchdog key locally sends its siblings back to defaults),
    # and game_loop's own config() agrees with it on the same two files. Two implementations of
    # one merge that disagreed would make a local override mean different things in different
    # components, which is the bug this function's own docstring is a story about.
    ("watchdog._merged_config -> the local config override is invisible to the watchdog",
     ".game_loop/bin/watchdog::_merged_config", "    return {}\n",
     ["config.local", "override", "watchdog", "waiting"], None, 20),
    # THIN AT 2 -> 3, and this is the one the report meant. notify.py reimplements the config
    # merge for exactly one purpose: the project label on every page it sends, which is how a
    # human reading one Slack channel knows WHICH repo woke them. The structural check
    # elsewhere proves notify.py MENTIONS config.local.json; nothing proved it HONOURS it —
    # which is the war story in this function's own docstring, one component down.
    ("notify._merged_config -> paging never sees the local override",
     ".game_loop/bin/notify.py::_merged_config", "    return {}\n",
     ['and config', 'every component', 'every executable', 'notify names'],
     # THIN at 1: this reader feeds only the project NAME used in page text, so one assertion
     # notices. Its siblings in the guards and the watchdog carry the load.
     "one consumer -- the project name in page text; the deciding readers are elsewhere", 3),
]

# Every candidate producer that is NOT swept, and WHY. Default-deny: a name that is in neither this
# mapping nor MUTANTS fails the run.
#
# A reason here is load-bearing prose and the one thing a reader can check. Two kinds live here:
#
#   EXCLUDED — the "nothing" is not a withheld finding. A helper whose None means "git failed" and
#   whose behaviour is swept through its callers; a resolver whose None is a LOUD refusal; a
#   formatter whose empty list is a configured opt-out. These are correct and stay.
#
#   KNOWN GAP — it is a producer, it should be swept, and it is not. Say that. An honest "not yet"
#   is worth more than a false exclusion, and the false exclusion is the failure mode this whole
#   default-deny shape exists to prevent: it looks identical to a decision and never gets revisited.
#
# Writing "not a real producer" for something that is one clears the list and re-creates the bug.

# ---- THE TUPLE-SHAPED PRODUCERS (#76 fallout) --------------------------------------------------
# Every one of these was OUTSIDE the denominator until `_returns_nothing` learned to look inside a
# returned tuple. They are listed together because they were discovered together and their floors
# are measured in one run — a floor is only comparable to one taken with the same instrument, which
# this file learned the expensive way.
#
# FLOORS MEASURED at aff65a3 — one instrument, one 38.3-minute run of all 67 producers, never
# rounded and never guessed. Two sweeps were KILLED before this one rather than recording their
# numbers: a peer session pushed to main mid-run both times, changing test/run.py by 355 and then 67
# lines, and a kill count is a set difference over the WHOLE suite — so those numbers would have
# described a tree nobody would ever have. That is this file's own rule about comparability, paid
# rather than quoted.
#
# TWO OF THE THREE ARE MEASURED NOW, and the correction is seventy-fold.
#
# ci_commands and ci_gap came back at 222 kills each. I explained that with "blast radius" and set
# floors below it. Re-measured after fixing the crash: THREE each. There was no blast radius; there
# was an IndexError in test/run.py — `_out.split("NO GATE RUNS")[1]` raises the moment a neutered
# producer removes that marker, which ended the run, and every assertion that never printed `ok`
# was counted as killed. Floors 2, one below the reading.
#
# _scan_transcript and five others still crash on a different shape — an unguarded READ rather than
# an unguarded split — and stay NOT MEASURED with floor 0. read_or_empty/json_or_none convert the
# sites; the next sweep says whether that was all of them.
#
# THE ORIGINAL COMMENT, kept because the lesson is the comment rather than the numbers:
#
# ci_commands 222, ci_gap 222, _scan_transcript 171. I saw three readings far outside every other
# producer's range, invented "blast radius" to explain them, and then set floors BELOW readings I had
# just decided were inflated — a number being managed rather than taken. A consumer said so plainly:
# the rationalisation was doing the work the measurement should have.
#
# Re-measured with NOT MEASURED in place, all three came back NOT MEASURED: the suite never finished
# under those mutants, so every assertion that never ran counted as killed. There was no reading to
# explain. Their floors are 0 and owed, along with five more that were producing the same inflated
# numbers unnoticed.
#
# The lesson is not about these three. It is that an explanation which fits a number is not evidence
# the number exists.
#
# THE CRASH CLASS, and why five producers are still outside this denominator.
#
# An assertion whose CONDITION raises ends the run, and every assertion after it never prints `ok` —
# which a set difference over passing NAMES counts as KILLED. A crash therefore scored as maximum
# coverage. Three shapes, each found by neutering one producer and reading the traceback:
#
#   1. an unguarded split/index on output the producer shapes      -> after_marker
#   2. an unguarded read of a file the producer writes             -> read_or_empty / json_or_none
#   3. the producer's NOTHING-ARM flowing into something that cannot take None
#      (.replace(None), `x in None`, None[..], getsize of an absent file)  -> dig / `or ""`
#
# ci_commands and ci_gap went 222 -> 3 when shape 1 was fixed; upstream_check went NOT MEASURED ->
# 12 when 2 and 3 were fixed at its sites.
#
# FIVE REMAIN, and they are not one site each: fixing the first site in refresh_handoff and in
# _scan_transcript revealed a second behind it. There is NO systemic fix — `check(name, cond)`
# evaluates its condition at the CALL SITE, so nothing inside check() can catch the raise without
# making all 1369 conditions lazy.
#
# THE METHOD, ~9 minutes per site: archive HEAD, neuter with neuter(), run test/run.py, read the
# traceback, guard that site, repeat until the run prints a summary line.
#
# PAID at 87b40d2, the sweep AFTER the one that found them UNPROTECTED: pin_status 2,
# running_host_version 3, _upstream_fetch 1. Thin, and honestly thin — few arms by nature.
#
# retro_nudge went 1 -> 5, and its floor rises 3 -> 4. The one floor here that moves UP, and it is
# legitimate: it came back below floor, was chased instead of lowered, was found to have real
# coverage loss, and got six assertions plus a mutate --prove. Measured on the run after that fix,
# with the same instrument.
#
# remote_has_ref STAYS 0 and came back UNPROTECTED, for a reason worth writing down: the assertions
# I wrote for mark_publication_state STUB IT OUT, so they exercise the reporter and leave the thing
# reported on unmeasured. Covering the report and not the reported thing is the trick every stamp in
# this project hides behind — and I did it inside a fix for exactly that. Three direct assertions
# against a real bare remote now exist; its floor is owed by the next sweep.
# PROMOTED OUT OF NOT_SWEPT, where it arrived with #100 declaring "this is a producer that SHOULD
# be swept and is not yet". The assertions that kill it exist now, so the debt is payable rather
# than restatable.
#
# RAW 6, FLOOR 3, AND THE DIFFERENCE IS NOT ROUNDING. Three of the six kills are collateral and say
# nothing about whether a handover stands a watchdog down:
#
#   ...names each planted orphan rather than only the first     ) all three are reachability
#   ...and the check FINDS one when there is one                ) bookkeeping: the neutered body
#   no shipped function is reachable ONLY from the suite        ) drops the only product caller of
#                                                               ) watchdog_pid_identity, so it
#                                                               ) reads as an orphan
#
# The three that are genuinely about this producer are the three that name its behaviour: the live
# process is killed, the note does not claim a stop that never happened, and it says why it could
# not check. A kill needs a named killer, and the name has to be about the guard — a count alone
# would have recorded 6 here and called an orphan-detector's opinion coverage of a SIGTERM.
MUTANTS += [
    ("disarm_watchdog -> a handover signals nothing, and reports that it did",
     ".game_loop/bin/_gl_impl.py::disarm_watchdog", "    return None, \"none was armed\"\n",
     ['names each', 'sentence as', 'check FINDS', 'handover says', 'watchdog already', 'no shipped'],
     "raw 6, genuine 3 — the other three are reachability assertions firing because the neutered "
     "body orphans watchdog_pid_identity, not because they know anything about handovers.", 8),
]

MUTANTS += [
    ("verify.scope_arg -> every commit gate silently un-scopes to the whole tree",
     ".game_loop/bin/verify::scope_arg", "    return None\n",
     ["--scope-from", "scope-from", "OUT OF SCOPE", "in this commit"],
     "11 kills, MEASURED by the sweep of 2026-08-26 against ec9be33b — the first reading this producer has ever had. It was excluded as a KNOWN GAP whose blocker was 'not in HEAD yet', which expired at 8561324 and went unnoticed: an exclusion is a decision about a moment, and nothing watches for the moment to pass. Promoting it was the right call — neutered it un-scopes every commit gate back to the whole tree, and 11 assertions notice.", 11),
    ("verify.outside_scope_tail -> a scoped run stops saying what it did NOT look at",
     ".game_loop/bin/verify::outside_scope_tail", '    return ""\n',
     ["OUT OF SCOPE", "not in this commit", "stay dirty", "not even looked at"],
     "3 kills, and only ONE of them is about this producer. THIS NOTE USED TO BE ITS SIBLING'S: the text describing `scope_arg` — 11 kills, un-scoping every commit gate back to the whole tree — was copy-pasted here when the two entries were added together, and sat for a day claiming a number and a behaviour that belong to a different function. A reader would have concluded this one measures 11 and un-scopes commit gates; it does neither. That is the 'mislabelled, not stale' class this file warns about, caught by reading the killers rather than by comparing counts. WHAT IS ACTUALLY TRUE: of its 3 kills, two are the sweep's own bookkeeping (`neuter` reaches every candidate, every declared producer mutates its own file) and redden for ANY neutered producer at all. The single genuine killer — a scoped pass NAMES the dirty paths outside the commit — is SHARED with `scope_arg`, so it passes whenever either works. This producer's own coverage is one assertion it does not own exclusively, and that is the finding the borrowed note hid.", 2),
]

MUTANTS += [
    ('_latest_version -> the source repo never appears to have a newer commit',
     '.game_loop/bin/_gl_impl.py::_latest_version', '    return None\n',
     ['update check', 'newer commit', 'latest sha', 'update_cache'],
     "19 kills, MEASURED by the full sweep of 2026-08-26 against d2411ab — the first reading this producer has ever had. It entered the denominator with #115's widening of the candidate finder, and its first two sweeps produced NOTHING: the declared mutant body carried a literal backslash-n instead of a newline, so the file did not parse and the suite could not start. Comparable: test/run.py is unchanged between that sweep's tree and this commit.", 19),
    ('inv_oneline -> status carries no INV summary line at all',
     '.game_loop/bin/_gl_impl.py::inv_oneline', '    return ""\n',
     ['INV:', 'INVARIANTS', 'inv summary', 'one-line INV'],
     "6 kills as of 2026-08-27; it read 5 when MEASURED by the full sweep of 2026-08-26 against d2411ab — the first reading this producer has ever had. It entered the denominator with #115's widening of the candidate finder, and its first two sweeps produced NOTHING: the declared mutant body carried a literal backslash-n instead of a newline, so the file did not parse and the suite could not start. Comparable: test/run.py is unchanged between that sweep's tree and this commit.", 5),
    ('limits_summary -> the usage window is never summarised for status',
     '.game_loop/bin/_gl_impl.py::limits_summary', '    return None\n',
     ['limits:', 'usage window', '5h ', '7d '],
     "3 kills, MEASURED by the full sweep of 2026-08-26 against d2411ab — the first reading this producer has ever had. It entered the denominator with #115's widening of the candidate finder, and its first two sweeps produced NOTHING: the declared mutant body carried a literal backslash-n instead of a newline, so the file did not parse and the suite could not start. Comparable: test/run.py is unchanged between that sweep's tree and this commit.", 3),
    ('log_kinds -> the writable record kinds come back empty',
     '.game_loop/bin/_gl_impl.py::log_kinds', '    return {}\n',
     ['kinds', 'log_kinds', 'dead kind', 'record kind'],
     "14 kills, MEASURED by the full sweep of 2026-08-26 against d2411ab — the first reading this producer has ever had. It entered the denominator with #115's widening of the candidate finder, and its first two sweeps produced NOTHING: the declared mutant body carried a literal backslash-n instead of a newline, so the file did not parse and the suite could not start. Comparable: test/run.py is unchanged between that sweep's tree and this commit.", 14),
    ('_accumulates_then_returns -> the accumulator shape is never recognised',
     'test/mutation_sweep.py::_accumulates_then_returns', '    return False\n',
     ['producers this', 'ARE unreachable', 'a candidate', 'producer actually', 'every excluded'],
     "5 kills, MEASURED by the full sweep of 2026-08-26 against d2411ab — the first reading this producer has ever had. It entered the denominator with #115's widening of the candidate finder, and its first two sweeps produced NOTHING: the declared mutant body carried a literal backslash-n instead of a newline, so the file did not parse and the suite could not start. Comparable: test/run.py is unchanged between that sweep's tree and this commit.", 5),
    ('candidates -> the sweep finds no producers to account for',
     'test/mutation_sweep.py::candidates', '    return []\n',
     ['candidate', 'denominator', 'unaccounted', 'producer'],
     "9 kills, MEASURED by the full sweep of 2026-08-26 against d2411ab — the first reading this producer has ever had. It entered the denominator with #115's widening of the candidate finder, and its first two sweeps produced NOTHING: the declared mutant body carried a literal backslash-n instead of a newline, so the file did not parse and the suite could not start. Comparable: test/run.py is unchanged between that sweep's tree and this commit.", 9),
    ('source_files -> the sweep sees no source to enumerate',
     'test/mutation_sweep.py::source_files', '    return []\n',
     ['source set', 'tracked source', 'extensionless', 'denominator'],
     "10 kills, MEASURED by the full sweep of 2026-08-26 against d2411ab — the first reading this producer has ever had. It entered the denominator with #115's widening of the candidate finder, and its first two sweeps produced NOTHING: the declared mutant body carried a literal backslash-n instead of a newline, so the file did not parse and the suite could not start. Comparable: test/run.py is unchanged between that sweep's tree and this commit.", 10),
    ('prun.main -> the parallel runner always reports success',
     'test/prun.py::main', '    return 0\n',
     ['silent shard', 'SUM OVER', 'distinct assertion', 'suite floor'],
     "8 kills, MEASURED by the full sweep of 2026-08-26 against d2411ab — the first reading this producer has ever had. It entered the denominator with #115's widening of the candidate finder, and its first two sweeps produced NOTHING: the declared mutant body carried a literal backslash-n instead of a newline, so the file did not parse and the suite could not start. Comparable: test/run.py is unchanged between that sweep's tree and this commit.", 8),
    ('assertions_that_cannot_fail -> no assertion looks unfailable',
     'test/run.py::assertions_that_cannot_fail', '    return []\n',
     ['falls back to a literal True', 'cannot fail', 'else True'],
     "2 kills, MEASURED by the full sweep of 2026-08-26 against d2411ab — the first reading this producer has ever had. It entered the denominator with #115's widening of the candidate finder, and its first two sweeps produced NOTHING: the declared mutant body carried a literal backslash-n instead of a newline, so the file did not parse and the suite could not start. Comparable: test/run.py is unchanged between that sweep's tree and this commit.", 2),
]

MUTANTS += [
    ("_write_section_map -> a FAST sweep has no map and silently runs everything",
     "test/mutation_sweep.py::_write_section_map", "    return\n",
     ["section map WRITER", "maps every declared producer", "writes beside the MODULE"],
     "5 kills on the full sweep of 2026-08-27 against 3b00196; it read 2 when measured by hand on the way out of NOT_SWEPT, and the assertions around it have grown since. This was the repo's LAST declared KNOWN GAP and its entry stated the gap exactly: neutering produces no map, every FAST producer falls back to the whole suite, and the result is correct-but-slow with nothing to notice. Two things were owed and both landed before this moved -- the sweep SAYS SO now when FAST is asked for with no map, and the writer is DRIVEN by assertions rather than only read in source. I claimed this move in 322a6cd's commit message and did not make it: the prerequisites shipped and the entry stayed. The next sweep still reporting KNOWN GAPS (1) is what caught it, which is the instrument outliving my own summary of it.", 5),
]

MUTANTS += [
    ("fast_without_map_notice -> a FAST run with no map goes quiet again",
     "test/mutation_sweep.py::fast_without_map_notice", '    return ""\n',
     ['notice that', 'for FAST', 'producer actually'],
     "2 kills, and the two that CANNOT kill it are the point: two of its four assertions check that "
     "it stays SILENT (a map present, and a non-fast run), which `return \"\"` satisfies. A producer "
     "whose job is to speak only sometimes can only be killed by the arms where it speaks. Measured "
     "by neutering it, not estimated.", 2),
]

MUTANTS += [
    ("floor_breaches -> a short run reports no breach, which is what a short run already looked like",
     "test/prun.py::floor_breaches", "    return []\n",
     ['with fewer', 'assertion short', 'producer actually'],
     "2 kills, and the reason it is not 5 is worth more than the number: three of the five "
     "assertions on this function check that it returns NOTHING — a met floor, an absent floor, a "
     "boolean that must not count as one. `return []` satisfies every one of them. A neutered "
     "function that returns the SAFE value passes every negative-arm test by construction, so a "
     "kill count here can only ever come from the arms that expect it to SPEAK. Measured by "
     "neutering it and running the section, not estimated.", 2),
]

MUTANTS += [
    ("note_line -> a floor nobody explained renders exactly like one somebody accepted",
     "test/mutation_sweep.py::note_line", "    return None\n",
     ["why it is thin", "NOT STATED", "unprotected", "what it covers"],
     "4 kills, and one of them is weaker than the other three: the header check reads this file's "
     "SOURCE for the phrase rather than calling the function, so it would flip for any edit that "
     "moved that text. The other three drive the function. Measured against the working tree at a "
     "1601 baseline — this producer is not in HEAD yet, so the archive could not carry it.", 4),
]

# THE EMPTY-STRING PRODUCERS, MEASURED. Surfaced when `_returns_nothing` learned that "" is a
# nothing; measured 2026-08-25 against the working tree (they were not in HEAD as candidates when
# measured) at a 1696 baseline, via this file's own neuter()/run()/in_a_copy_of(). Two of the five
# came back at ZERO and stay out with their reasons above — one of them because its branch is
# unreachable (#108), which is a different finding from being unasserted.
MUTANTS += [
    ("duplicate_key_tail -> a manifest key declared twice merges in SILENCE",
     ".game_loop/bin/verify::duplicate_key_tail", '    return ""\n',
     ["duplicate", "declared", "merge", "manifest"],
     "5, and one of them is not mine: `--coverage says it too` was already asserting this report "
     "through a different door, which is why the producer was covered while being invisible.", 5),
    ("_closing -> the stop gate reads an empty closing, so nothing is ever a promise",
     ".game_loop/bin/_gl_impl.py::_closing", '    return ""\n',
     ['and PAST-TENSE', 'QUOTED marker', 'backticks and', 'plain PRESENT-tense', 'blocks announce-then-stop', 'the TAIL'],
     "5 of 6, and the sixth CANNOT flip: it asserts the empty input returns empty, which a neuter "
     "to `return \"\"` satisfies by definition. Worth the entry because the first version of these "
     "six scored 1 — five were written as ABSENCES (`marker not in result`), and an absence is the "
     "cheapest thing a broken producer gives you. Rewritten to pin what SURVIVES, same six "
     "assertions, 1 -> 5.", 5),
    ("unchecked_tail -> files no rule claims are never named",
     ".game_loop/bin/verify::unchecked_tail", '    return ""\n',
     ['GREEN --check', 'a passing'], None, 2),
    ("assist -> the flair line for a verb is always empty",
     ".game_loop/bin/flair.py::assist", '    return ""\n',
     ["flair", "assist"],
     "2, and flair is the one file where fun lives — a low floor here costs a joke, not a verdict.",
     2),
]

# ── THE WATCHDOG PIDFILE CARRIES AN IDENTITY (#102) ──────────────────────────────────────────
# MEASURED AT THE WORKING TREE, not at HEAD, and said here because a floor whose provenance is
# unstated is a number. These producers did not exist in HEAD when measured, so the archive the
# sweep normally runs could not carry them; the run used this file's own neuter(), run() and
# in_a_copy_of() over the tracked working copy. Baseline 1596 named assertions passing. The next
# full sweep re-measures them the ordinary way and THAT number supersedes these.
#
# THE BODIES ARE NOT ALL `return None`, ON PURPOSE. For a predicate whose None means "could not
# tell", neutering to None collapses onto the SAFE direction — do not signal — which is what most
# of these assertions already expect, so almost nothing can flip. Both directions were measured
# rather than argued:
#
#   pid_is_ours -> None    1 kill    only the positive newest-wins assertion flips
#   pid_is_ours -> True    3 kills   every "left alone" assertion flips
#
# `return True` is the mutant worth keeping: it is the direction that DOES harm — signalling a pid
# nothing proved was ours, which is the bug this producer exists to prevent. A floor taken against
# the None mutant would have recorded 1 and called the safe direction thin coverage, when what it
# actually measured was that the failure mode is fail-safe.
MUTANTS += [
    ("pid_is_ours -> every pid in the file is treated as ours, and is signalled",
     ".game_loop/bin/watchdog::pid_is_ours", "    return True\n",
     ['identity differs', 'LOGGED as', 'whose recorded', 'GONE pid', 'no identity', 'NO recorded'],
     "the opposite neuter (`return None`) scores 1, not 3: None IS the safe direction here, so it "
     "flips only the newest-wins kill. Both were measured; this body is the harmful direction.", 8),
    ("read_pidfile -> the pidfile never yields a pid, so nothing is ever stood down",
     ".game_loop/bin/watchdog::read_pidfile", "    return None, None\n",
     ['was superseded', 'LOGGED as', 'watchdog whose', 'the previously'], None, 4),
    ("_proc_start -> no process has a readable start time, so no pid can be identified",
     ".game_loop/bin/watchdog::_proc_start", "    return None\n",
     ['identity differs',
      'a matching',
      'pid that',
      'GONE pid',
      'pid reports',
      'the previously'],
     "one kill, and it is the RIGHT one. A start time nobody can read makes every pid "
     "unverifiable, and unverifiable means NOT SIGNALLED — so the three assertions about leaving a "
     "process alone still pass, because they were already expecting that outcome. Only the paired "
     "positive (a matching identity IS signalled) can flip, and it does. Thin here measures the "
     "shape of the fallback, not a gap in it.", 8),
    ("watchdog_pid_identity -> game_loop's side can never identify a pid either",
     ".game_loop/bin/_gl_impl.py::watchdog_pid_identity", "    return None\n",
     ['handover says', 'watchdog already'],
     "same fail-safe shape as _proc_start above, one file over: an unreadable start time reads as "
     "'that pid had already exited', so the handover declines to signal and says so. The two that "
     "flip are the live kill and the note that must not claim a stop that never happened.", 2),
]

MUTANTS += [
    ("_stop_verdict -> the stop gate always allows the turn to end",
     ".game_loop/bin/_gl_impl.py::_stop_verdict", "    return True, \"\", None\n",
     ["stop gate", "stop_verdict", "turn-end", "mandate"], None, 40),
    ("waiting_verdict -> the watchdog never sees a run as waiting",
     ".game_loop/bin/watchdog::waiting_verdict", "    return False, \"\"\n",
     ["waiting", "watchdog", "subagent", "idle"], None, 12),
    ("upstream_check -> the upstream watcher reports nothing, ever",
     ".game_loop/bin/_gl_impl.py::upstream_check", "    return [], \"off\"\n",
     ["#76", "upstream"], None, 16),
    ("ahead_of_upstream -> never sees an unpushed commit",
     ".game_loop/bin/_gl_impl.py::ahead_of_upstream", "    return 0, None, None\n",
     ["unpushed", "upstream", "ahead"], None, 5),
    ("working_tree -> never resolves a worktree",
     ".game_loop/bin/_gl_impl.py::working_tree", "    return None, None\n",
     ['different REPOSITORY', 'naming BOTH', 'worktree it'], None, 3),
    # THIN AT 2 -> 4, measured against the RECORDED body `return False, None`. All three original
    # arms passed ONE root, so none of them could see that this answers about the TREE IT WAS
    # HANDED. worktree_drift() calls it twice — this tree and the main checkout — and publishes the
    # two as separate porcelain fields, so a version that ignored its argument would fill both from
    # one tree, and the report would claim the trees agree whenever the caller's own happened to be
    # pinned. Two roots now, and two PINNED roots at different shas, which is the case that
    # "equal shas means these two agree" actually rests on.
    ("pin_status -> never reports a tree as pinned",
     ".game_loop/bin/_gl_impl.py::pin_status", "    return False, None\n",
     ['pin marker', 'answers about', 'two PINNED', 'a VERSION', 'producer actually'], None, 4),
    ("probe_reading -> a probe's output never yields a reading",
     ".game_loop/bin/_gl_impl.py::probe_reading", "    return {}, None\n",
     ['an envelope', 'the OLDER', 'the envelope'], None, 3),
    ("running_host_version -> the running host's version is never known",
     ".game_loop/bin/_gl_impl.py::running_host_version", "    return None, \"neutered\"\n",
     ['path carrying', 'is taken', 'but carrying', 'producer actually', 'running_host_version with'], None, 3),
    ("_scan_transcript -> the transcript never yields records",
     ".game_loop/bin/_gl_impl.py::_scan_transcript",
     "    return [], {\"lines\": 0, \"skipped\": 0, \"oversized\": 0, \"denials\": {}}, None\n",
     ["transcript", "denial", "oversized"], None, 15),
    ("vacuous_rules -> a rule matching nothing is never named",
     ".game_loop/bin/verify::vacuous_rules", "    return [], None\n",
     ['tree git', 'not resurrect', 'rule matching', 'an EXEMPTION'], None, 5),
    ("ci_commands -> CI's commands are never read",
     ".game_loop/bin/verify::ci_commands", "    return [], \"neutered\"\n",
     ['gate runs', 'gate DOES', 'producer actually', 'NO workflows'], None, 5),
    ("ci_gap -> no CI command is ever reported as ungated",
     ".game_loop/bin/verify::ci_gap", "    return [], \"\"\n",
     ['gate runs', 'names each', 'check FINDS', 'gate DOES', 'no shipped', 'NO workflows'], None, 8),
    ("milestones -> flair never marks a milestone",
     ".game_loop/bin/flair.py::milestones", "    return [], []\n",
     ["flair", "milestone"], None, 4),
    ("_limitgate_verdict -> the limit gate always allows the turn to end",
     ".game_loop/bin/_gl_impl.py::_limitgate_verdict", "    return True, None\n",
     ["limit gate", "limit", "window", "gate denies", "context deny"], None, 19),
    ("_last_assistant_text -> the closing message is never recoverable",
     ".game_loop/bin/_gl_impl.py::_last_assistant_text", "    return None, \"neutered\"\n",
     ["closing message", "assistant text", "stop gate", "launder"], None, 8),
    ("merge_files -> a merge never yields the paths it touched",
     ".game_loop/bin/_gl_impl.py::merge_files", "    return None, \"neutered\"\n",
     ["merge", "attribute", "merge-base"], None, 10),
    ("wake_path_report -> a mandate with no external wake path is never named",
     ".game_loop/bin/_gl_impl.py::wake_path_report", "    return []\n",
     ['the internal', 'as WEAKER', 'DECLARED wake', 'mandate armed', 'producer actually'], None, 13),
    ("guards_report -> a disabled project guard is never reported as inert",
     ".game_loop/bin/_gl_impl.py::guards_report", "    return []\n",
     ["#90", "INERT", "UNKNOWN", "guard"], None, 12),
    # THIN AT 1 -> 4. Three assertions called it and TWO asserted `== []`, which a dead producer
    # returns too. Added: TWO dead triggers both named (reporting one of two is the failure the
    # reporter actually hit — they made the mistake twice and nothing corrected the first), and
    # triggers_report() driven, which NOTHING had driven. Finding that out cost a wrong assumption
    # worth recording: the dead-kind loop sits after an early `return []` for projects with nothing
    # wired, so a stray script in triggers.d with no trigger configured is never reported at all.
    ("trigger_dead_kinds -> a trigger matching an impossible kind is never named",
     ".game_loop/bin/_gl_impl.py::trigger_dead_kinds", "    return []\n",
     ['TWO dead', 'matters in', 'dead condition', 'kind NOTHING', 'producer actually'], None, 4),
    # THIN AT 2 -> 6. Four assertions and two of them asserted `== ([], None)` — not pinned, and
    # pinned to identical bytes — which the neutered form returns for everything. Added: TWO
    # differing files BOTH named (being shown one of two reads as the whole finding, and the guard
    # you were not shown stays inert after you re-pin), and pinned_report() DRIVEN, which nothing
    # had driven — so the finding could have stopped reaching the page with every assertion above
    # still green. The report must also say the state MEANS those edits are inert, and must say
    # COULD NOT COMPARE rather than going quiet on the unanswerable case.
    # MOVED OUT OF NOT_SWEPT BECAUSE ITS EXCLUSION WAS FALSE, not merely stale. The reason read
    # "sweeping pinned_report would sweep this, and it has not been done yet". pinned_report was
    # swept today at floor 150 — and _git_sha still killed NOTHING, measured. So the sentence was
    # wrong about coverage, not just out of date, and this file's own rule is that what is not
    # allowed is an exclusion that is not true. Found by scanning every exclusion reason for
    # remedy-shaped language rather than for the KNOWN GAP marker, which is showrunner's finding:
    # the debt hides in the reasons that do NOT carry the marker.
    #
    # 0 -> 2, and THIN on purpose rather than padded. It feeds the PINNED CODE block's second sha,
    # which is the only thing that can notice the pinned copy is a DIFFERENT COMMIT from the repo —
    # the state where every edit to .game_loop/bin/ here is inert until you re-pin, which is how a
    # fix sits unused while its author watches the guard not change. Nothing asserted it at all.
    # The third arm written (same commit -> no warning) is an absence arm and does not flip.
    ("_git_sha -> the repo's own commit is never resolved",
     ".game_loop/bin/_gl_impl.py::_git_sha", "    return None\n",
     ["PINNED CODE", "repo @", "DIFFERENT commit"], None, 2),
    ("pin_file_drift -> the pinned copy never differs from this tree",
     ".game_loop/bin/_gl_impl.py::pin_file_drift", "    return [], None\n",
     ['TWO differing',
      'reader who',
      'state MEANS',
      'report says',
      'be COMPARED',
      'THE STATE'], None, 6),
    # THIN AT 1, NOW 4, and the reason it was thin is the reason THIN is worth reporting. Four
    # assertions called this producer and THREE asserted it returns None — mid-work is silent, an
    # unpushed commit is silent, an already-marked HEAD is silent. All three are worth having (a
    # gate that fires always is the one that gets routed around) and none of them is coverage: a
    # dead producer returns None too, so they hold either way. Only the one positive arm flipped.
    #
    # What was added is what a dead producer cannot fake: the TUPLE is read rather than its
    # truthiness — the sha and level of the mark it is behind, and a count that goes to 2 on a
    # second stranded commit — and the CONSEQUENCE is driven end to end, a real handback refused
    # with the mark and HEAD named. Two of the arms here used to grep cmd_checkpoint's SOURCE for
    # "release_deferred", which reads identically whether or not the verb does it.
    ("release_owed -> finished unreleased work is never owed at the handback",
     ".game_loop/bin/_gl_impl.py::release_owed", "    return None\n",
     ['the COUNT', 'owed NAMES', 'THE CASE', 'handback in'], None, 4),
    ("release_distance_warning -> the handback never says the release is behind",
     ".game_loop/bin/_gl_impl.py::release_distance_warning", "    return None\n",
     ['the CONFIDENCE', 'is measuring', 'CANNOT know', 'three commits'], None, 5),
    ("work_since_last_block -> nothing ever counts as work after a refusal",
     ".game_loop/bin/_gl_impl.py::work_since_last_block", "    return None\n",
     ["#82", "laundering", "stop_after_block", "reworded"], None, 5),
    ("deferral_in_checkpoint -> a checkpoint never names a successor",
     ".game_loop/bin/_gl_impl.py::deferral_in_checkpoint", "    return None\n",
     ["#81", "deferral", "next action"], None, 12),
    ("upstream_review_nudge -> nobody is ever asked whose defect a learning was",
     ".game_loop/bin/_gl_impl.py::upstream_review_nudge", "    return None\n",
     ["#78", "upstream review", "TOOL's behaviour"], None, 8),
    ("hardens_since_review -> no learning is ever counted as unreviewed",
     ".game_loop/bin/_gl_impl.py::hardens_since_review", "    return [], True\n",
     ["#78", "baseline", "threshold"], None, 12),
    ("selected_tests -> a run's test count is never readable",
     ".game_loop/bin/_gl_impl.py::selected_tests", "    return None, \"neutered\"\n",
     ["#85", "selected", "ZERO tests", "count"], None, 8),
    # THIN AT 2 -> 6, and it has SEVEN outcomes: live, inert, and FOUR different unknowns.
    # Exactly one was asserted, end-to-end, in a producer whose entire job is telling apart
    # answers that look identical from outside. Driven directly now with an injected runner,
    # and the four unknown reasons are required to be PAIRWISE distinct — the reporter's own
    # first cut matched the marker inside a PARSE ERROR and called a dead anchor live, which
    # is the confusion those reasons exist to prevent.
    #
    # Two things went wrong writing it, both caught by measuring. My "no-parse" fixture took
    # the MID-LINE branch instead and never reached the branch it was for. And the
    # distinctness assertion was `len(set(reasons)) == 4`, which PASSED while two situations
    # shared a reason — five entries, four distinct values. A cardinality test over a set
    # cannot say WHICH pair collided, so it reads clean for a collision plus a spare, which
    # is the shape it was written to catch. Pairwise and by name now.
    ("mutation_liveness -> the probe never establishes anything",
     ".game_loop/bin/_gl_impl.py::mutation_liveness", "    return \"unknown\", \"neutered\"\n",
     ["#80", "liveness", "INERT"], None, 6),
    # THIN AT 2 -> 3, and the number depends on WHICH mutation, which nearly cost me the floor.
    # Two added assertions: that it asks the REMOTE rather than the local tag list (both tags in
    # the old fixture existed locally, so a `git tag` reader would answer them identically — delete
    # the local one and a local reader gets a PUBLISHED tag exactly backwards), and that a pushed
    # COMMIT is NOT a match because this asks --tags. That second boundary once made the release
    # gate answer False for every pushed commit and fire never: a function answering the
    # NEIGHBOURING question, the most common way a check here goes quiet.
    #
    # I MEASURED IT FIRST AGAINST `return None` AND GOT 4. The body recorded here is `return False`,
    # and against THAT the pushed-COMMIT arm holds — a dead producer returns False too. Same
    # assertions, same producer, different mutation, different coverage. A floor is only a fact
    # about the mutation it was taken against, so measure the body this line actually names.
    ("remote_has_ref -> the remote never has the ref",
     ".game_loop/bin/_gl_impl.py::remote_has_ref", "    return False\n",
     ['NO remote', 'the REMOTE', 'remote_has_ref finds'], None, 3),
    ("_upstream_fetch -> every upstream repo reads as unreachable",
     ".game_loop/bin/_gl_impl.py::_upstream_fetch", "    return None, None, \"neutered\"\n",
     # THIN AT 1 -> 3. Three assertions called it and two asserted `issues is None and why` — which
     # the neutered form, returning one canned "neutered" for everything, satisfies exactly. That is
     # showrunner's point in this repo's own file: NON-EMPTY AND INFORMATIVE ARE DIFFERENT CLAIMS,
     # and a reason identical across causes names the function rather than the finding. The three
     # real failures now have to stay DISTINGUISHABLE FROM EACH OTHER: no gh on PATH, gh ran and
     # failed, and gh exited 0 while printing non-JSON — the last being the one most easily read as
     # "nothing to report", since the process itself reported no error at all.
     ['SUCCEEDS while', 'answers yields', 'two failure', 'producer actually'], None, 3),
    ("read_probe -> notify never reports whether replies can be read",
     ".game_loop/bin/notify.py::read_probe", "    return False, \"neutered\"\n",
     ['the reply-read', 'every executable', '--test names', '--test verifies'], None, 3),
    # MEASURED AT 3, AND MEASURED AGAINST THE WORKING TREE rather than against HEAD — said here
    # because a floor whose provenance is unstated is a number. This producer did not exist in HEAD
    # when it was measured, so the archive the sweep normally runs could not carry it; the run used
    # this file's own neuter() and run(), over the tracked working copy, with the assertions held
    # still. Baseline 1329 named assertions passing (9 failing in a tree with no .git, outside the
    # denominator for the usual reason: they cannot flip). The next full sweep re-measures it in the
    # ordinary way, and THAT number supersedes this one.
    #
    # The three that flip are the three arms worth having: the successor observed live, the boot
    # grace, and the log line that says which ring a ring was. The fourth assertion — that a
    # handover nobody took up still RINGS — deliberately does NOT flip, because a neutered producer
    # returns None and ringing is what None already means. That is the safe direction by design.
    ("handed_off_quiet -> a handover never stands the predecessor's watchdog down",
     ".game_loop/bin/watchdog::handed_off_quiet", "    return None\n",
     ['names each', 'check FINDS', 'which ring', 'the boot', 'no shipped', 'predecessor stands'], None, 8),
]

# THE FAN-OUT BRAKE'S TWO PRODUCERS, MEASURED AGAINST THE WORKING TREE rather than against HEAD —
# said here because a floor whose provenance is unstated is a number. Neither exists in HEAD yet, so
# the archive main() builds could not carry them; the run used this file's own neuter() and run()
# over the tracked working copy, with the assertions held still. Baseline 1674 named assertions
# passing (11 failing in a tree with no .git, outside the denominator for the usual reason: they
# cannot flip). The next full sweep re-measures both in the ordinary way, and THOSE numbers
# supersede these.
#
# BOTH FLIP THE SAME SIX, and that is a fact about the call graph rather than a coincidence:
# binding_spawn_block asks _spawn_verb_hit which verb was typed, so neutering either one removes the
# refusal entirely. Reported as two producers because they fail independently — a broken matcher and
# a broken threshold are different bugs with the same symptom.
#
# The six are the six worth having: the refusal itself, the handoff NOT opening it, both threshold
# orders, the configured verb list, and the interpreter-argument match. The fail-open assertions
# deliberately do NOT flip — a neutered producer returns None, which is ALLOW, and "not denied" is
# what allow already means. That is the safe direction by design, and the same shape as
# handed_off_quiet above.
MUTANTS += [
    ("_spawn_verb_hit -> no command ever names a configured fan-out verb",
     ".game_loop/bin/_gl_impl.py::_spawn_verb_hit", "    return None\n",
     ["spawn", "fan-out", "brake", "verb", "cap"], None, 6),
    ("binding_spawn_block -> a session of any size may keep starting Crawlers",
     ".game_loop/bin/_gl_impl.py::binding_spawn_block", "    return None\n",
     ["spawn", "fan-out", "brake", "handoff", "cap"], None, 6),
]


# ── HANDOVER CHAINS ─────────────────────────────────────────────────────────────────────────────
#
# Floors measured 2026-08-25 against the working tree that introduced them, whole-suite on both
# sides (no section map is generated in this checkout, so every reading here is the default
# denominator rather than a subset).
#
# TWO OF THESE FIRST READ 1554, AND THE READING WAS A CRASH WEARING A COVERAGE NUMBER. The suite's
# own --json assertion called json.loads on a verb that printed PROSE when there were no chains; it
# raised, the run stopped there, and every later assertion vanished from the mutant's passing set —
# which a set difference reports as kills. 1554 of 1757 is not a well-covered producer, it is the
# collapsed baseline this file already warns about, arriving through a new door: not a crash in the
# archived TREE this time, but one an assertion caused under a mutant. The fix was in the product
# (--json now answers in the empty case, because a consumer told to expect JSON must not be handed a
# sentence exactly when there is nothing to report) and the honest floors came back 10 and 8.
MUTANTS += [
    ("handover_edges -> the log yields no handovers, so no chain can be walked",
     ".game_loop/bin/_gl_impl.py::handover_edges", "    return []\n",
     ["chain", "thread", "hop", "handed"], None, 10),
    ("handover_chains -> the edges never join into chains",
     ".game_loop/bin/_gl_impl.py::handover_chains", "    return []\n",
     ["chain", "thread", "hop", "listing"], None, 8),
    ("thread_for -> a successor never finds the chain it was handed into",
     ".game_loop/bin/_gl_impl.py::thread_for", "    return None\n",
     ['session handed', 'that WAS', 'producer actually'], "THIN AT 2, and the two are the whole contract: a handed-to "
     "session INHERITS its chain, and a session handed to twice takes the LATEST pointing. The "
     "third branch — nobody handed to me, so mint — is asserted through successor_thread's output "
     "rather than here, because from the outside 'returned None' and 'minted a new chain' are the "
     "same observable and only the minting one is worth a name.", 2),
    ("tab_label -> the tab never derives a label, and falls back to the noun",
     ".game_loop/bin/_gl_impl.py::tab_label", '    return ""\n',
     ['subject far', 'producer actually', 'no --task'], "THIN AT 2 because the function is two decisions wide: trim to "
     "the tab's width, and keep the ellipsis that stops a trimmed label reading as a complete "
     "smaller job. Both are asserted. The third case — no subject at all, fall back to "
     "'successor' — cannot kill this mutant, since the neutered form produces exactly that "
     "fallback, and an assertion that passes under the mutation is not coverage of it.", 2),
]


# ── READING THE SUCCESSOR'S REAL SESSION ID BACK ────────────────────────────────────────────────
#
# Floors measured 2026-08-25 against the working tree that introduced them, whole-suite on both
# sides, the same denominator as the block above.
#
# These two are one read split in half — the directory listing, and the delta over it — so their
# kill sets overlap almost entirely. That is worth saying rather than reading as two independent
# measurements: neuter either and the successor's id stops being recovered, which is the same
# observable from outside.
MUTANTS += [
    ("_saggar_presence_names -> saggar's presence directory reads as absent",
     ".game_loop/bin/_gl_impl.py::_saggar_presence_names", "    return None\n",
     ['printed as', 'miss says', 'id RECORDED', 'two terminals'], "the outer half of the read: with no listing there is no delta, so every discovery path collapses to the one honest answer — no directory to read. What survives it is the fallback, which is asserted separately: the handover is still recorded, and the minted id is what gets written.", 4),
    ("_saggar_discover -> the successor's real id is never read back, and the minted one stands",
     ".game_loop/bin/_gl_impl.py::_saggar_discover", '    return None, ""\n',
     ['printed as', 'miss says', 'no presence', 'id RECORDED', 'two terminals'], "the inner half: the terminal opens, the file appears, and nothing looks. Its kills are the three outcomes that have to be told apart — an id read back, two terminals that cannot be told apart, and a miss that says COULD NOT CONFIRM rather than DID NOT START.", 5),
]


# ── guardtest (#91.2), measured the day it shipped. ────────────────────────────────────────────
# RAW COUNTS MINUS THE ACCOUNTING KILLS, per the trap documented at the top of this list — and the
# trap has a SECOND face nobody had written down. Undeclared, these fail three bookkeeping
# assertions on their own; declared, they fail a different one, "every declared producer actually
# MUTATES its own file", because a producer already neutered no longer changes bytes when its
# declared body is applied. The artefact does not disappear on declaration, it CHANGES SIDES.
#
#   undeclared raw:  hook_decision 10   guardtest_directions 3   run_guard_case 9   (-3 accounting)
#   declared   raw:  hook_decision 11   guardtest_directions 6   run_guard_case 10  (-4 accounting)
#
# The -4 is one assertion counted in four shards, not four assertions: prun's totals are a SUM OVER
# SHARDS and a shared-fixture outcome re-runs in each shard that needs it. Both routes land on the
# same floors — 7, 2, 6 — which is the only reason to trust either subtraction. Measured both ways
# on 2026-08-27 rather than predicted once.
MUTANTS += [
    ("hook_decision -> every hook reads as having said nothing at all",
     ".game_loop/bin/_gl_impl.py::hook_decision", '    return None, ""\n',
     ['permissionDecision', 'silent', 'never deny', 'REAL guard', 'allow was reached',
      'does NOT establish', 'script that does nothing'],
     "the two refusal protocols collapse into one unreadable answer. This is the exact confusion "
     "that made the first cut of guardtest read this repo's own write guard, mid-refusal, as "
     "allowing everything — the verb built to catch a guard going quietly inert was about to "
     "certify one. RE-MEASURED 2026-08-27 after the pretty-print fix: 7 -> 13 distinct, because "
     "that fix added the JSON-scan, unreadable and glyph assertions. A floor measured against a "
     "function that has since changed is a floor for code nobody runs.", 13),
    ("guardtest_directions -> every fixture claims to exercise both directions",
     ".game_loop/bin/_gl_impl.py::guardtest_directions", '    return True, ""\n',
     ['all expect ALLOW', 'all expect DENY'],
     "the both-controls rule stops discriminating, so a fixture whose cases all point one way is "
     "accepted and a script that does nothing at all passes its own guard suite. Two kills, one "
     "per direction, which is the whole of what this producer decides.", 2),
    ("run_guard_case -> every case reports a match without running anything",
     ".game_loop/bin/_gl_impl.py::run_guard_case", '    return "match", "", None\n',
     ['allow was reached', 'script that does nothing', 'cannot be EXECUTED'],
     "the harness passes unconditionally. Its kills are the three answers that must stay apart: a "
     "real match, a guard that has gone inert, and a script that could not be executed at all — "
     "the last of which is not a verdict about the guard in either direction.", 6),
    ("guardtest_bad_expects -> every fixture's expectations read as understood",
     ".game_loop/bin/_gl_impl.py::guardtest_bad_expects", "    return []\n",
     ['FIXTURE defect', 'END TO END', 'NO case was run'],
     "a misspelt `expect` goes back to being reported as a FAILING GUARD. Measured at 1 when only "
     "the function was asserted; the two end-to-end checks took it to 3, and those exist because "
     "disconnecting the die() that calls this left the unit checks passing while the verb blamed a "
     "guard that was behaving correctly.", 3),
]


MUTANTS += [
    ("authorizations_report -> the balance of a paid-for grant is invisible again",
     ".game_loop/bin/_gl_impl.py::authorizations_report", "    return []\n",
     ['live grant is NAMED', 'SPENT grants are COUNTED', 'guard call SPENDS one'],
     "status stops showing what is LEFT of an `authorize` grant. Measured at 3, one per decision "
     "the report makes: name the live ones with their balance, COUNT the spent ones rather than "
     "listing them (never-authorized and authorized-and-exhausted are different answers), and say "
     "that running a guard spends one. Its fourth arm — silence when there are no grants — cannot "
     "be killed by this mutant, because returning [] is exactly what that arm does.", 3),
]


# ── #117's parse gate. ─────────────────────────────────────────────────────────────────────────
MUTANTS += [
    ("_parses -> every source reads as compilable",
     "test/mutation_sweep.py::_parses", "    return True\n",
     ['DOES NOT PARSE', 'parse check answers BOTH ways'],
     "the question stops discriminating, so the gate below it can never fire. It was measured at 1 "
     "and killed by the SAME single assertion as its only caller — identical results from "
     "neutering either, which is the shape of a helper nothing measures on its own. A direct "
     "assertion that it answers both ways took it to 2 and gave it a killer of its own.", 2),
    ("declared_mutant_unparseable -> a malformed mutant body reads as fine",
     "test/mutation_sweep.py::declared_mutant_unparseable", '    return False, ""\n',
     ['DOES NOT PARSE'],
     "3 kills as of the clean sweep of 2026-08-27 (it read ONE when this note was written, and the note said so long after that stopped being true — a count in prose beside the count the line already prints is a second copy that drifts). The ceiling is still structural in the way that matters: the FUNCTION is driven directly by four assertions here, but its VERDICT is only observable "
     "through sweep_one, which is a closure inside main() and cannot be driven — the same "
     "untestable-by-construction problem this file solved for probed_verdict by lifting it out. "
     "Lifting sweep_one is a larger change than the gate it would test, so the debt is recorded "
     "here rather than paid quietly or hidden.", 3),
]


# ── the wake-landed report (#95 proposal 4's observable half), measured 2026-08-27. ────────────
# COUNTED BY DISTINCT NAME, not by prun's total. prun shards the suite and its footer says so in
# terms — "the totals above are a SUM OVER SHARDS" — so a shared-fixture assertion is counted once
# per shard that needs it. Reading kills straight off that total gave wake_landed_lines 11 for 7
# real assertions, and the sweep's own baseline is stated in NAMED assertions. An instrument
# cruder than its subject, in the measurement whose entire job is being exact.
#
#   _minutes_since      raw 8  · accounting 4 · shard dupes 2 · distinct 2
#   wake_landed_lines   raw 15 · accounting 4 · shard dupes 4 · distinct 7
MUTANTS += [
    ("_minutes_since -> every timestamp reads as unparseable",
     ".game_loop/bin/_gl_impl.py::_minutes_since", "    return None\n",
     ['unreadable timestamp', 'how old that is'],
     "the age of the last landed wake stops being readable. Its kills are the two that matter: an "
     "age actually rendering, and the THIRD answer for a stamp nobody can parse — which must not "
     "render as 0, the freshest possible reading, in the one case where nothing is known.", 5),
    ("wake_landed_lines -> status says nothing about wakes that arrived",
     ".game_loop/bin/_gl_impl.py::wake_landed_lines", "    return []\n",
     ['NO WAKE HAS LANDED', 'how old that is', 'CANNOT see', 'arrivals COUNT'],
     "the whole report disappears while the wake-path DECLARATION stays — which is the pre-#95 "
     "state exactly: a declared path, nothing said about whether it has ever delivered, and an "
     "inert run reading identically to a healthy one. HELD AT 7, AND OWED A RAISE TO ~16. The cadence check added an OVERDUE verdict, a holding verdict and a third answer for no cadence, each asserted — a hand measurement says 16. The floor stays at 7 until a FULL SWEEP re-measures, because the killer record still describes the older function and this repo's own gate refuses a floor that claims more than the record holds. It refused this raise, which is the gate working. Four of its seven kills are the #95 report "
     "itself; the other three are the orphan check, which fires because this is the only shipped "
     "caller of _minutes_since — real collateral, and named here so the number is not a mystery.", 12),
]


NOT_SWEPT = {
    # ── THE EMPTY-STRING NOTHINGS, enumerated the day the detector started seeing them ──────────
    # `_returns_nothing` did not count `""`, so these thirteen were never candidates: not swept, not
    # excluded, NEVER ENUMERATED. Same hole as the tuple-payload one, different shape. They are
    # decided here rather than left to appear as a silent gap.
    #
    # THE SUITE'S OWN HELPERS. Neutering one of these does not measure whether anything asserts its
    # behaviour — it measures HOW MANY ASSERTIONS USE IT, which is a popularity count wearing a
    # coverage verdict. `read_or_empty` alone would redden hundreds and report as the best-covered
    # producer in the repo while proving nothing about itself. That is the collateral-kill problem
    # with no genuine kills left underneath it.
    "test/run.py::read_or_empty": "a suite helper: neutering it reddens every assertion that reads "
        "a file, which counts users rather than coverage of the helper",
    "test/run.py::after_marker": "a suite helper, same reason — and its own contract (raise rather "
        "than split, so a renamed heading fails HERE) is asserted directly elsewhere",
    "test/run.py::why": "a suite helper for rendering a refusal reason in a message",
    "test/run.py::_marks_section_rendered": "a suite helper, and it became a candidate only "
        "because a guard was added INSIDE it: it now returns False early when the stubbed seam "
        "was never reached, which is the check that stops it verdicting on a run nobody "
        "controlled. Neutering it to False would fail the one assertion that calls it — that is "
        "the assertion doing its job, not a coverage reading about this helper.",
    "test/run.py::_gate_ran": "a suite helper: it reads the stop gate's own payload probe so a "
                              "permissive assertion requires the gate to have RUN, not merely to "
                              "have exited 0. Neutering it makes every assertion that uses it FAIL "
                              "rather than pass, which is the safe direction and the reason it is "
                              "not swept — the same call as read_or_empty above.",
    "test/run.py::_ssctx": "a suite fixture helper",
    "test/run.py::_ask": "a suite fixture helper",
    "test/run.py::_pol": "a suite fixture helper",
    "test/run.py::_guard_note": "a suite fixture helper that reads a guard's additionalContext",
    "test/run.py::_guard_block": "a suite fixture helper that reads a guard's refusal",
    #
    # KNOWN GAPs — product producers that SHOULD be swept and are not yet. Declared so the run
    # reports them every time rather than letting the number sit at a comfortable 0 undecided.
    
    
    ".game_loop/bin/_gl_impl.py::run_verify_check": "MEASURED AT 0 AND THAT IS NOT A COVERAGE GAP "
        "(#108). It answers whether a gated file's checks are stale, and its only caller reaches it "
        "three lines AFTER `--mark` has already died on an unclean tree — so the input it reads "
        "(changed_files, 'what a commit would actually carry') is empty by construction and it can "
        "only ever return \"\". Not unasserted: its finding branch has no reachable caller. Writing "
        "an assertion would mean asserting an outcome the code cannot produce. Excluded until #108 "
        "decides whether the check should read the COMMIT's diff instead, or whether the clean-tree "
        "gate is doing all the work and the text should say so.",

    

    "test/run.py::dig": "walks a nested structure and returns None at the first missing key, "
            "inside the suite. It exists so a guarded read whose result is SUBSCRIPTED fails one "
            "assertion instead of ending the run — the crash that kept six producers outside this "
            "denominator even after the read guards landed. Neutering it to a constant None makes "
            "every assertion that reads producer-written state fail at once, which those "
            "assertions catch directly.",
    "test/run.py::_multi_symbol_producers":
        "the scan for producers whose literals can yield more than one symbol, nested inside main(). Same ground as _vanishing and _writes_outside_tmp: it is the instrument, so neutering it measures the suite's own scan rather than the tool. It became visible to the candidate finder only with #115's widening; it always had this shape. THE SECOND HALF OF THIS REASON HAS EXPIRED and is recorded rather than deleted: it used to say `neuter` could not reach an indented def anyway, which was true when written and stopped being true when neuter became indent-aware. The reach gap is closed and every candidate is now reachable, so the ONLY reason this is excluded is the first one — it is the instrument. An exclusion is a decision about a moment, and this is the third time in one day that a reason outlived its moment here.",
    "test/run.py::_whole_file_log_readers":
        "the scan for assertions that read a whole log file, nested inside main() — same ground and the same single reason: it is the instrument. It used to say 'same reach problem' too; that half expired when neuter became indent-aware.",
    "test/run.py::_vanishing": "the scan for assertions that can silently not run, inside the "
                               "suite. Its empty list is the ordinary answer — the file has no "
                               "such site — and it is the one producer here whose OWN positive "
                               "control is asserted beside it, against the real commit where the "
                               "case existed. Mutating the instrument voids the reading rather "
                               "than measuring it",

    "test/run.py::_segments": "the selector's own index of sections, inside the suite. Neutered "
            "to an empty list, every --section matches nothing and the selector REFUSES with exit "
            "2 rather than running zero checks and reporting success — and that is asserted "
            "directly beside it, in both directions: a real selection must exit 0 and print its "
            "summary, a nonsense one must exit 2. Mutating the instrument voids those readings "
            "rather than measuring them.",

    "test/run.py::_stale": "the scan for --section patterns in verify.yaml that no longer match a "
            "section, inside the suite. Its empty list is the ordinary answer — the manifest is "
            "clean — and like _vanishing it is asserted through its OWN positive control standing "
            "next to it: the very next check calls it on a pattern naming no section and requires "
            "that pattern back. Neutered to always-empty that control fails; neutered to "
            "always-nonempty the clean check fails. Mutating the instrument voids both readings "
            "rather than measuring them.",

    "test/run.py::_scope": "the selector's free-variable analysis — which names a run of "
            "statements binds, and which it reads from an enclosing scope. Neutered to two empty "
            "sets the closure finds NO dependencies and every subset shrinks to the sections "
            "literally named, which is the unsound direction and exactly the kind of quiet "
            "shrinkage this file exists to catch. It is caught directly beside it instead: the "
            "two assertions on the closure read BOTH arms of the answer — a free name that must "
            "be present (OUTER) and bound names that must be absent (a helper's parameters, a "
            "comprehension target) — so an empty answer fails on the first and a saturated one "
            "fails on the second. Inside the suite, and mutating the instrument voids those "
            "readings rather than measuring them.",

    "test/run.py::json_text": "the same guard as json_or_none for the other door — a JSON STRING "
                              "rather than a path. Its None arm is the honest answer for a hook "
                              "that printed nothing, which is what a neutered producer makes them "
                              "do; two crash sites were reached that way. Inside the suite, and "
                              "mutating the instrument voids the reading rather than measuring it",

    "test/run.py::json_or_none": "the same shape for parsed JSON, inside the suite, and its None "
            "arm is also the honest answer for a file that exists and is corrupt. Asserted through "
            "the cases that read producer-written state rather than by mutation.",
    "test/mutation_sweep.py::run_detail": "the sweep's own suite-runner — this exclusion was "
            "carried over from `run`, which is now a one-line wrapper around it and no longer a "
            "producer at all. The reason is unchanged. Its None arm is the DEADLINE — a mutant "
            "that hangs measures nothing and waiting for it measures nothing — and it is asserted "
            "directly in-suite against a command that sleeps past its bound and one that returns "
            "normally. Mutating it would measure whether the sweep can sweep its own subprocess "
            "call, which is not a question about this repo's gates. The exit status and stderr it "
            "now also returns are what `died_how` reads; that one IS swept.",

    ".game_loop/bin/_gl_impl.py::_ledger_last": "reads the last timestamp out of the ledger (#78). "
            "Neutered to None it declares every project un-baselined, which the FIRST-ENCOUNTER "
            "assertion catches directly and loudly; neutered to a constant it breaks the counting "
            "the threshold assertions drive. Both arms are asserted through the producers above "
            "rather than through this reader, which is where the behaviour actually lives.",

    ".game_loop/bin/_gl_impl.py::_py_parses": "ast.parse with a boolean face (#80). Neutered to a "
            "constant it either passes every mutated file — which the COULD-NOT-PROVE assertion "
            "beside it catches directly — or refuses every one, which the ✓ PROVED assertion "
            "catches. Both arms are asserted in-suite against real broken and real valid sources, "
            "which is a stronger check than mutation and does not need the same name twice.",
    ".game_loop/bin/_gl_impl.py::run_source": "a closure inside cmd_mutate that writes a source, runs "
            "the test and restores — it is the probe's I/O, not a producer that reports by "
            "silence. Its restore path is asserted directly (the tree is intact after every "
            "refusing path), which is the property worth pinning.",
    ".game_loop/bin/_gl_impl.py::cmd_mutate": "the verb itself, whose every outcome is driven end to "
            "end by the #80 assertions — unparseable, inert, unprobeable and proved, each with the "
            "tree checked afterwards. Neutering the command body would fail all of them at once "
            "and measure nothing finer than 'the verb runs'.",

    ".game_loop/bin/_gl_impl.py::_git": "pure git helper — None means git failed, not a finding withheld; swept through its "
            "callers (config_paths_report, main_checkout, release_owed, and ahead_of_upstream, "
            "which is how unpushed_warning reaches it), which assert the "
            "git-failed arm beside the git-worked one",
    ".game_loop/bin/_gl_impl.py::_git_out": "pure git helper for an arbitrary tree — None is 'no such ref / not a repo', a "
                "mechanical outcome. Its callers are attribution_tree, merge_files and "
                "cmd_attribute, which turn every one of those Nones into a STATED refusal that "
                "`game_loop attribute` is asserted on; none of them can report by silence",
    ".game_loop/bin/_gl_impl.py::_rev": "pure git helper — None is 'that ref does not resolve'. Its one caller is cmd_self "
            "(`self --pin`), which dies on it; that refusal is loud and asserted, never silent",

    ".game_loop/bin/_gl_impl.py::_saggar_agent": "asks saggar to open the successor's terminal (#79), "
            "reporting through a (started, detail) pair. Its DECLINE arm is asserted directly "
            "in-suite: `successor` is run for real with saggar absent from PATH, and the run must "
            "say NOTHING STARTED, name the shim remedy, send the reader to the printed command and "
            "still exit 0. A mutant that returns started=True is killed by that assertion. "
            "ITS SUCCESS ARM CANNOT BE ASSERTED HERE AT ALL, and that is the whole reason this is "
            "an entry rather than a MUTANTS line: the only way to observe it is to let it open real "
            "claude terminals on the machine running the tests, which is the one thing a suite "
            "asserting nothing was started must never do. It was proved once by hand instead — "
            "called live, `saggar list` went 4 terminals to 5 — and that proof is point-in-time, "
            "not a check that runs again. So this is a STATED GAP over one half of one function, "
            "not a claim that the function is covered. AMENDED: the success BRANCH is now exercised "
            "in-suite by the handover tests, which put a stub `saggar` that exits 0 on PATH and "
            "assert the handover is recorded behind it. That proves the branch is wired, and it "
            "still proves nothing about the app — the stated gap is now the app half only.",

        ".game_loop/bin/watchdog::_age_sec": "seconds since an ISO stamp. Its None is 'that stamp does "
            "not parse', a mechanical outcome and not a finding withheld. Its one caller is "
            "handed_off_quiet, which turns None into the LOUD direction — fall through and ring, "
            "with `watchdog_handover_gone` in the log — and both of that caller's arms are asserted "
            "there. Sweeping this would measure whether datetime.fromisoformat works.",

    # --- EXCLUDED: the "nothing" is the LOUD direction. These resolvers refuse by returning None,
    # and the refusal is a die() in the caller that the suite asserts many times over. Neutering
    # them makes every claim refuse — noisy, not silent. The silent failure here is the INVERSE
    # (resolving a path it should not), and that mutation is outside this sweep's shape (INV6).
    ".game_loop/bin/_gl_impl.py::resolve_read": "its None is the REFUSAL, and the refusal is loud — `claim --read` dies on it "
                    "and that death is asserted repeatedly. The silent direction is the inverse "
                    "(resolving what it should not), which is the mutation this sweep cannot make",
    ".game_loop/bin/_gl_impl.py::resolve_env": "same as resolve_read, for a pin's anchor: None makes the command die, which is "
                   "asserted; the dangerous direction is accepting an anchor that does not exist",

    # --- EXCLUDED: loaders and normalizers. Their "nothing" is a state the caller branches on, not
    # a report that was withheld.
    ".game_loop/bin/_gl_impl.py::sanitize_session": "a normalizer, not a detector — None means 'not a usable session id' and "
                        "the caller branches to repo-global state. Neutering it changes WHICH "
                        "state file is used, which fails loudly across the session-scoping tests",
    ".game_loop/bin/_gl_impl.py::load_limits": "a loader — None is 'no limits file yet', the ordinary first-run state, and the "
                   "callers already treat it as empty ((load_limits() or {}))",
    ".game_loop/bin/_gl_impl.py::installed_version": "a loader — None is 'no VERSION file', which is documented as the "
                         "game_loop source repo's own state. The producer that turns this into a "
                         "verdict is update_notice, and that IS swept",
    ".game_loop/bin/_gl_impl.py::_scan_text": "None is a STATED skip ('too big to grep'), not a finding withheld; --expect "
                  "reports UNCHECKED rather than ✓ when it gets nothing, and that is asserted",

    # --- EXCLUDED: a formatter whose silence is configured, and helpers of producers now swept.
    ".game_loop/bin/_gl_impl.py::flair_lines": "a formatter, and its empty list is a configured opt-out (no flair module) "
                   "rather than a verdict about anything. Silence here is the shipped default",
    ".game_loop/bin/_gl_impl.py::_home_keyed": "helper of config_paths_report — its None is 'not under anyone's home', the "
                   "ordinary case for every entry. config_paths_report is a MUTANTS entry and "
                   "sweeps both of this helper's arms",
    ".game_loop/bin/_gl_impl.py::main_checkout": "helper of worktree_drift — its None is 'this IS the main checkout', the "
                     "ordinary case. worktree_report is a MUTANTS entry",
    ".game_loop/bin/_gl_impl.py::_same_bytes": "helper of worktree_drift — its None is UNREADABLE, which worktree_report turns "
                   "into an explicit UNKNOWN line rather than 'matching'; swept there",
    ".game_loop/bin/_gl_impl.py::admit_distribution": "its verdict is delivered by die(), not by this return value — the "
                          "return is the record it writes afterwards. Neutering the body deletes "
                          "those refusals and would re-measure the dominance gate, which already "
                          "has its own MUTANTS entry",

    ".game_loop/bin/_gl_impl.py::authorize_recurrence": "a pure reader over log.jsonl — [] means no "
                          "prior grant carries these words, never a finding withheld. Swept "
                          "through its only caller, recurrence_lines, which asserts both arms "
                          "beside each other: a first grant silent, a second distinct path loud",

    # --- KNOWN GAPS. Real producers. Should be swept. Are not, and the reason is cost, not merit:
    # each MUTANTS entry is one full suite run (~1 min), and this change spent its budget on the
    # four report producers that were actually found weak. These are the queue, in this order.
    # --- FOUND ONLY BY THE SECOND SIGNATURE. Both were invisible while the discriminator looked
    # for a literal empty return, which is why the accounting read "0 unaccounted" over a short
    # denominator. Neither is excluded on merit; both are queued.

    # ── THE MEASURING INSTRUMENT ITSELF ─────────────────────────────────────────────────────────
    # Now that the denominator is every source file (#44), the suite and this sweep are inside it.
    # They are excluded on ONE shared ground, and it is not "they are only tests": neutering the
    # instrument does not make a measurement safe, it makes the measurement MEANINGLESS. Every
    # assertion flips because none of them ran, and the count reads as perfect coverage of
    # something nobody exercised.
    #
    # DECLARED out-of-scope rather than left absent, which is the whole argument of the chain this
    # closes: "not mine to test" and "never noticed" produce the same empty result, and only one of
    # them is a decision.
    "test/run.py::main":
        "the suite's own entrypoint. Neutered, no assertion runs at all, so the entire baseline "
        "flips and the number measures the mutation of the ruler rather than of any producer.",
    "test/run.py::atattributed":
        "a helper inside the suite, reached only from main() — same ground: mutating the instrument "
        "voids the reading instead of testing anything.",
    "test/run.py::_embedded_python":
        "the extractor for the embedded-python check, inside the suite and reached only from "
        "main(). Neutered it finds NO programs, so its own gate passes over an empty set — and the "
        "assertion that there ARE programs to check is the guard against exactly that, sitting "
        "beside it where a reader will see it.",
    ".game_loop/bin/_gl_impl.py::session_models":
        "reads the running model off the transcript. Its None is one of two REPORTED outcomes — "
        "model_report says 'could not be read' rather than staying quiet — and all four arms "
        "(found, last-wins, synthetic-skipped, absent) are asserted directly against a fixture "
        "transcript. Neutering it to None reproduces the arm the suite already drives.",
    ".game_loop/bin/_gl_impl.py::write_model_verdict":
        "publishes the model verdict where a PARENT reads it. Its None means there was nothing to "
        "publish (no transcript, or no session), which the caller reports rather than swallows — "
        "`model` dies naming the reason. The file's existence and content are asserted directly in "
        "a sandbox, including the changed:true case, so sweeping it would re-measure through the "
        "verb what two paired assertions decide at the source.",
    ".game_loop/bin/_gl_impl.py::model_report":
        "the status lines for the model. Its empty return means 'nothing declared and nothing "
        "readable', which is the OPTIONAL case and is asserted as its own arm alongside the match "
        "and the mismatch. Sweeping it would re-measure through status what three paired "
        "assertions decide at the source.",
    ".game_loop/bin/_gl_impl.py::absorb_rate_limits":
        "the one place a rate_limits reading becomes the snapshot, extracted so the statusline tap "
        "and the spawned probe cannot drift apart. Its return is the windows dict, never a silence: "
        "an empty result means the reading carried no usable window, which the callers report "
        "explicitly. Neutering it to {} reproduces the no-windows path the suite already drives "
        "through both callers, and its paging and carry-forward behaviour are asserted through the "
        "statusline tests that predate the extraction.",
    ".game_loop/bin/_gl_impl.py::_windows_claim_live":
        "the positive-control half of one conditional-absence exercise. Its None is one of three "
        "reported outcomes rather than a silence, and all three are asserted directly in a sandbox: "
        "no control fired, confirmed live, and FALSIFIED by an unexpected window. Neutering it to "
        "None reproduces the first arm the suite already drives on purpose.",
    ".game_loop/bin/_gl_impl.py::_statusline_claim_live":
        "the EXERCISE behind one host claim: it returns a description of live evidence, or None "
        "when there is none. Its None is not a silence-on-pass — it is one of two reported "
        "outcomes, and both are asserted directly in a sandbox (observed, not-observed, and a "
        "snapshot missing the field). Neutering it to None reproduces the arm the suite already "
        "drives on purpose, so sweeping it would re-measure through status what three paired "
        "assertions decide at the source.",
    ".game_loop/bin/_gl_impl.py::installed_by":
        "reads the packager marker that decides WHICH upgrade command the update notice names. "
        "Neutered it returns None and the notice falls back to the curl — which is precisely the "
        "reported defect, and the suite asserts both branches by behaviour: a marked install must "
        "name the packager's command and must NOT offer the curl, an unmarked one must offer the "
        "curl and say what it does. Sweeping it would re-measure through status what two paired "
        "assertions already decide directly.",
    "test/behaviour_gate.py::changed_lines":
        "returns the changed refusal lines, or None when git could not answer. The None is not a "
        "silence-on-pass: the caller reports 'no verdict' on it and returns 0 deliberately, because "
        "a gate that cannot diff must not claim clean OR fail a commit it never examined (INV5 — it "
        "guards the very files whose fix would be blocked). Its empty-list and non-empty paths are "
        "both asserted directly in a sandbox git repo, four ways including a non-refusal edit to "
        "the same watched file.",
    "test/run.py::_trigger_target":
        "extracts the file a configured trigger runs, inside the suite. Its None means an inline "
        "command with no file, which is legitimate and skipped rather than reported — and the "
        "can-it-fire arm beside it feeds a path that does not exist and asserts the extractor "
        "returns it, so a neutered version returning None would take that arm red.",
    "test/run.py::_writes_outside_tmp":
        "the scan that proves this file never writes to the working tree, and it lives inside the "
        "suite. Neutered it finds no writes, so its own gate passes over an empty set — which is "
        "why the arm beside it asserts there IS a write to find, and a third arm feeds it a "
        "deliberate offender. Both decide it in-suite rather than by mutation.",
    "test/run.py::cc_limits":
        "reads the limits block an install wrote, inside the suite. Its None is the ordinary "
        "answer — most of the cases it serves assert the installer wrote NOTHING — so neutering "
        "it to a constant None would make roughly half the context-cap arms pass vacuously and "
        "the other half fail. The arms are paired against each other rather than by mutation: "
        "every 'wrote nothing' assertion has a 'wrote exactly this' assertion beside it reading "
        "the same helper, so a reader stuck on one answer cannot satisfy both.",
    "test/run.py::_parses":
        "ast.parse with a boolean face, inside the suite. Neutered to a constant it either passes "
        "everything or fails everything, and the paired 'a deliberately broken program fails to "
        "parse' arm decides that in-suite rather than by mutation.",
    "test/mutation_sweep.py::all_candidates":
        "the accountant inside its own denominator. Neutered it returns NO candidates, so the "
        "coverage gate passes trivially over an empty set — the exact short-denominator failure "
        "this file exists to catch, self-inflicted.",

    # ── THE OTHER FOUR SCRIPTS (#44) ────────────────────────────────────────────────────────────
    # These were never excluded; they were ABSENT. The sweep parsed one file of five and reported
    # "0 unaccounted", and the number was true about a set that had quietly stopped containing them.
    # They are now inside the denominator and each carries a decision.
    #
    # Every one is a KNOWN GAP rather than a genuine exclusion, and the run prints them as such.
    # They are NOT measured yet: a first sweep of the ten stalled — at least one of these
    # mutations hangs the suite rather than failing it, which is its own finding and has to be
    # chased before a floor recorded here would mean anything. Recording an unmeasured floor would
    # be the exact target-making this file's header warns about.

    # ── THE COMMIT-SCOPE PRODUCERS, AND THE ORDERING THAT FORCES THEM HERE ───────────────────────
    # Both are new with the change that scopes the commit gate to what a commit CARRIES, and both
    # are KNOWN GAPS in the plainest sense: they should be swept, they are not yet, and the reason
    # is mechanical rather than a judgement. `main()` sweeps `git archive HEAD` — deliberately, so
    # a floor is measured against a tree somebody can check out — and these two functions are not
    # in HEAD until the commit that introduces them lands. There is no order of operations in which
    # a first sweep could have measured them, so a number recorded here today would be a number
    # about nothing.
    #
    # What IS known about them is not nothing, and is not a substitute for a floor: each is killed
    # by assertions written in the same change (the scoped `--check` wording, and the OUT OF SCOPE
    # line naming the dirty paths a narrowed run did not look at). That is an argument for
    # expecting a non-zero floor, not a measurement of one.
    #
    # WHEN PROMOTING THESE, the trap two entries up applies: a NOT_SWEPT member scores 2 free
    # accounting kills that DISAPPEAR on the move into MUTANTS, so measure, then subtract the two.
}


def _returns_nothing(ret):
    """Is this `return` handing back a NOTHING — bare, None, [], False?

    Deliberately crude, and conservative in the direction that matters: it is better to enumerate a
    function that is not really a producer than to miss one. A false candidate costs one line in
    NOT_SWEPT; a missed one is the entire bug default-deny exists to prevent.
    """
    v = ret.value
    if v is None:                                    # a bare `return`
        return True
    if isinstance(v, ast.Constant) and (v.value is None or v.value is False):
        return True
    if isinstance(v, ast.List) and not v.elts:        # `return []`
        return True
    # AND THE EMPTY STRING, which is how every REPORT in this project says "nothing to report":
    # duplicate_key_tail, unchecked_tail, run_verify_check, _closing. Thirteen producers whose
    # nothing is `""` sat outside the denominator entirely — not swept, not excluded, never
    # enumerated — which is the same hole this function closed for tuples one lesson ago, in a
    # different shape. Found 2026-08-25 from a sibling's rule about tolerances that are safe only in
    # a file's current state; `duplicate_key_tail` is the report that WARNS the next writer about a
    # merged duplicate key, and nothing anywhere had ever exercised it.
    if isinstance(v, ast.Constant) and isinstance(v.value, str) and v.value == "":
        return True
    # AND INSIDE A TUPLE, IN THE PAYLOAD SLOT (#76 fallout). A producer whose contract is
    # `(finding, why)` or `(allow, reason, log)` reports its nothing in the FIRST position —
    # `([], "off")`, `(False, "")`, `(None, "no EXECPATH")`. Reading only the outer node left every
    # such producer outside the denominator: not excluded, not listed, never enumerated. Eleven of
    # them, including the stop gate's own verdict and the watchdog's.
    #
    # FIRST ELEMENT, NOT ANY ELEMENT, and the difference is not style. `any` marks `(value, None)`
    # as a nothing — so a function whose every return carries a trailing None loses its OTHER arm,
    # `candidates()` stops seeing a pair, and the function DROPS OUT of discovery. Widening the
    # definition made the set SHRINK: `_home_keyed` was enumerated before the change and not after.
    # That is this file's own short-denominator bug committed inside the fix for it, caught by the
    # stale-exclusion gate rather than by me. The monotonicity assertion in test/run.py is the part
    # that stops it recurring: a change here may only ADD.
    return (isinstance(v, ast.Tuple) and bool(v.elts)
            and _returns_nothing(ast.Return(value=v.elts[0])))


def _accumulates_then_returns(fn):
    """Does this build into an empty local and return it?

    The FIRST signature — a literal empty return beside a non-empty one — misses this entirely,
    and this is how most real producers are written: seed a list, append findings, return it. There
    is one Return node and it hands back a Name, so nothing about it looks like "nothing".

    Reported by a downstream maintainer who applied the same design to their own library: the
    unfailable predicate that started this whole family lived in exactly this shape, in a validator
    that accumulated errors and returned the accumulator. Missing it here made THIS file's
    "0 unaccounted" vacuous in the worst direction — the accounting looked complete because the
    DENOMINATOR was short, which is a gap in the candidate set rather than in the exclusions and so
    shows up nowhere.
    """
    empties = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and isinstance(n.value, (ast.List, ast.Dict, ast.Set)) \
           and not getattr(n.value, "elts", getattr(n.value, "keys", [1])):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    empties.add(t.id)
    # THE RETURN EXPRESSION, WALKED — not the return NODE, type-checked. `isinstance(n.value,
    # ast.Name)` was the whole of it, so `return sorted(acc)`, `set(acc)`, `list(acc)` or
    # `sorted(set(acc))` hands back a Call rather than a Name and vanished from the candidate set.
    # That is the same defect this function was WRITTEN for, one wrapper further out: the shape was
    # widened and the SPELLING went on being pinned.
    #
    # Surfaced by the coverage gate staying silent for a producer ending `return sorted(set(out_))`
    # after failing loudly, days earlier, for one ending `return out_` — two functions differing by
    # a call wrapper. Measured: 163 candidates before, 173 after, and four of the ten newly visible
    # are shipped payload functions that report findings to a consumer, plus this file's OWN
    # `candidates` and `source_files`, which could not see themselves. (#115)
    for n in ast.walk(fn):
        if isinstance(n, ast.Return) and n.value is not None:
            if any(isinstance(x, ast.Name) and x.id in empties for x in ast.walk(n.value)):
                return True
    return False


def candidates(src):
    """Module-level functions that can return a FINDING or a NOTHING — the silence-on-pass shape.

    Both arms are required. A function that only ever returns nothing reports nothing; one that
    always returns a value cannot report by silence. It is the pair that makes a producer able to
    decline, and declining is the state an assertion cannot tell from working.
    """
    # WALK THE WHOLE TREE, not `tree.body`. Module level is not the same question as "every
    # function", and the difference is invisible until someone adds a class: the candidate set
    # silently shrinks while the accounting still reports 0 unaccounted. That is the SHORT
    # DENOMINATOR bug one level further out again — reported by a second downstream maintainer
    # after it reached their own enumerator, and live here rather than hypothetical: `limits_lock`
    # is a class and its two methods were outside the scan entirely.
    found = []
    for fn in [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)]:
        rets = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        if any(_returns_nothing(r) for r in rets) and any(not _returns_nothing(r) for r in rets):
            found.append(fn.name)
        elif _accumulates_then_returns(fn):
            found.append(fn.name)
    return sorted(found)


# A DECLARATION, not merely "it parses": ast.parse accepts JSON and YAML, which are valid Python
# expressions, so "parses as Python" pulled in settings.json, config.json and two templates — four
# config files in a seven-item list, and a list that is mostly noise is one people learn to skim.
SOURCE_DECLARES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)


def source_files(tree=None):
    """Every TRACKED file that is Python source, asked of git rather than enumerated here.

    Asking git is the point. Links 1-6 of this chain were all the same mistake — a list maintained
    by hand, complete on the day it was written — so the set has to come from a source of truth that
    changes when the repo does. A `*.py` glob is the other wrong answer: `game_loop`, `verify` and
    `watchdog` have no extension, and they hold the producers that matter most. Gating on
    extension-or-shebang instead would silently skip a Python file that has neither.
    """
    tree = tree or REPO
    r = subprocess.run(["git", "-C", tree, "ls-files"], capture_output=True, text=True)
    names = [ln.strip() for ln in r.stdout.split("\n") if ln.strip()] if r.returncode == 0 else []
    if not names:
        # NO GIT, NO SHORT DENOMINATOR. Falling back to the one file was the #44 bug reintroduced by
        # environment: an EXTRACTED tree -- which is exactly the shape a packager gates, and has no
        # .git -- would report "0 unaccounted" over a set of one, silently, in the place where the
        # accounting matters most. Walk instead. A tracked-file list and a walk of a clean
        # extraction are the same set, and the walk needs nothing.
        skip = {".git", "__pycache__", ".worktrees", ".game_loop_self", "node_modules", ".venv"}
        for root, dirs, files in os.walk(tree):
            dirs[:] = [d for d in dirs if d not in skip]
            for f in files:
                names.append(os.path.relpath(os.path.join(root, f), tree))
    out = []
    for rel in names:
        if not rel:
            continue
        try:
            with open(os.path.join(tree, rel)) as f:
                mod = ast.parse(f.read())
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
        if any(isinstance(n, SOURCE_DECLARES) for n in mod.body):
            out.append(rel)
    return sorted(out)


def all_candidates(tree=None):
    """{"<relpath>::<name>": (relpath, name)} across every source file.

    Keys are QUALIFIED BY FILE deliberately. A bare-name namespace cannot tell two implementations
    of one name apart — link 4 of this chain, found by a consumer whose two classes both defined the
    same method — and it would silently merge them into one decision.
    """
    tree = tree or REPO
    found = {}
    for rel in source_files(tree):
        try:
            with open(os.path.join(tree, rel)) as f:
                src = f.read()
        except OSError:
            continue
        for name in candidates(src):
            found[f"{rel}::{name}"] = (rel, name)
    return found


def unaccounted(found=None, mutants=None, not_swept=None):
    """Candidates that are neither swept nor explicitly excluded — the default-deny failure."""
    found = all_candidates() if found is None else found
    mutants = MUTANTS if mutants is None else mutants
    not_swept = NOT_SWEPT if not_swept is None else not_swept
    decided = {m[1] for m in mutants} | set(not_swept)
    return sorted(k for k in found if k not in decided)


def unreasoned(not_swept=None):
    """Exclusions with no reason attached. An exclusion with no reason is just a name on a list —
    unreadable, uncheckable, and indistinguishable from someone clearing the run."""
    not_swept = NOT_SWEPT if not_swept is None else not_swept
    return sorted(n for n, why in not_swept.items() if not (why or "").strip())


def decided_twice(mutants=None, not_swept=None):
    """Names that are BOTH swept and excluded. A contradiction, and a quiet one: the sweep would
    dutifully mutate the producer while the mapping says it was decided not to."""
    mutants = MUTANTS if mutants is None else mutants
    not_swept = NOT_SWEPT if not_swept is None else not_swept
    return sorted({m[1] for m in mutants} & set(not_swept))


def coverage_gate(found=None, out=print):
    """Default-deny over the producers in EVERY source file. Returns an exit code, and says what.

    Runs BEFORE the mutation runs, deliberately. This is a list-hygiene failure fixable in one line,
    and making somebody wait out a full sweep to be told about it is how a check earns a --skip.
    """
    found = all_candidates() if found is None else found
    contradictions, blank = decided_twice(), unreasoned()
    orphans = unaccounted(found)
    # An exclusion that outlives the function it excused is the denylist bug returning by the side
    # door: the name stays decided forever while nothing it referred to is in the script any more.
    stale = sorted(set(NOT_SWEPT) - set(found))
    if not (orphans or contradictions or blank or stale):
        return 0
    if orphans:
        out(f"UNACCOUNTED PRODUCERS — {len(orphans)} function(s) across "
            f"{len({k.split('::')[0] for k in found})} source file(s) can return a finding or a")
        out("nothing, and this sweep has no position on any of them: "
            + " · ".join(orphans))
        out("That is the denylist failure this file's own header argues against. Decide about each:")
        out("add it to MUTANTS, or to NOT_SWEPT with a reason — including the honest reason 'it is a")
        out("producer, it should be swept, it is not yet'. What is not allowed is silence about it.")
    for n in contradictions:
        out(f"DECIDED TWICE — {n} is in MUTANTS *and* NOT_SWEPT. One of them is a leftover.")
    for n in blank:
        out(f"EXCLUDED WITHOUT A REASON — NOT_SWEPT[{n!r}] is blank. The reason is the deliverable.")
    for n in stale:
        out(f"STALE EXCLUSION — NOT_SWEPT[{n!r}] names nothing this repo still produces. Renamed, or "
            "gone: either way the exclusion now excuses a function nobody can read.")
    return 1


def killers(baseline, still, marks):
    """(every assertion this mutation killed, the subset whose NAME is about this producer).

    LIFTED OUT rather than left inline for the reason the last one was: a classification nested in
    the sweep loop is reachable only by running a sweep, and "untestable by construction" is a
    construction you can remove. Matching is on the assertion NAME, lowercased, against the same
    `marks` the SURVIVED lines already use — one vocabulary per producer, not two.
    """
    killed_names = sorted(baseline - still)
    # BOTH SIDES LOWERCASED. Comparing a mark against `x.lower()` while leaving the mark as written
    # means any mark carrying a capital can never match — 18 of 302 here, across 15 producers, with
    # `INERT`, `PINNED CODE` and `ZERO tests` among them. The flaw was in the SURVIVED line below
    # first and I copied it into this function an hour after writing that one. It made five
    # producers report EVERY KILL COLLATERAL, which reads as a coverage finding and was a matcher
    # bug — a scan's first findings are evidence about the scan.
    return killed_names, [x for x in killed_names
                          if any(m.lower() in x.lower() for m in marks)]


def producer_liveness(original, fn, run_mutant):
    """Does the suite REACT to this producer crashing? ("live"|"inert"|"unknown", why).

    Only asked when a producer killed NOTHING, because that is the only verdict where the two
    causes are confusable — one kill already proves the code executes. `run_mutant(src)` returns
    the suite's output for a tree carrying `src`, or None when it did not finish.

    UNKNOWN IS A REAL ANSWER HERE and it has three distinct causes, kept apart on purpose: the
    probe could not be substituted, the run did not finish, or the suite went red WITHOUT the
    marker — which means something other than the probe broke it, and a verdict either way would
    rest on that unrelated breakage. `mutate --prove`'s probe was reported as a finding once for
    exactly the third case, before it demanded the marker.
    """
    poisoned, hit = neuter(original, fn, f'    raise SystemExit("{_PROBE_MARK}")\n')
    if not hit:
        return "unknown", "the probe body could not be substituted into this producer"
    out = run_mutant(poisoned)
    if out is None:
        return "unknown", "the probe run did not finish, so nothing was established either way"
    if _PROBE_MARK in out:
        return "live", "the suite surfaced the probe's raise, so this producer executes"
    if re.search(r"^\d+ passed, 0 failed", out, re.M):
        return "inert", ("the suite passed with a `raise` in this body — nothing calls it, or "
                         "nothing that does can see it fail")
    return "unknown", ("the suite went red WITHOUT the probe marker, so something other than the "
                       "probe broke it")


def in_a_copy_of(base, rel, run):
    """A runner that drops `src` at `rel` in a throwaway copy of `base` and runs the suite there."""
    def go(src):
        pt = tempfile.mkdtemp(prefix="sweep-probe-")
        try:
            shutil.copytree(base, pt, dirs_exist_ok=True)
            with open(os.path.join(pt, rel), "w") as f:
                f.write(src)
            os.chmod(os.path.join(pt, rel), 0o755)
            return run(pt)
        finally:
            shutil.rmtree(pt, ignore_errors=True)
    return go


def probed_verdict(original, fn, run_mutant):
    """(verdict, note) for a producer that killed NOTHING — INERT, or UNPROTECTED with a reason.

    LIFTED OUT OF THE SWEEP LOOP so it can be driven (lamp-owner's push, and my own INV6 note
    saying this wiring had never fired). It still has not fired on a real producer — no producer
    here kills zero — but the mapping from probe state to verdict is no longer reachable only
    through a branch nothing has taken. A nested closure is untestable by construction, and
    "untestable by construction" was the whole of the admission.
    """
    state, why = producer_liveness(original, fn, run_mutant)
    if state == "inert":
        return INERT, why
    if state == "unknown":
        return UNPROTECTED, "liveness UNESTABLISHED — " + why
    return UNPROTECTED, why


def _parses(src):
    """Does this source compile? A boolean, so the caller reads as a question (#117)."""
    try:
        ast.parse(src)
    except (SyntaxError, ValueError):
        return False
    return True


def declared_mutant_unparseable(original, mutated):
    """Did the DECLARED body stop this being a program? (bool, why) — #117.

    MODULE-LEVEL SO IT CAN BE DRIVEN, which is this file's own precedent: `probed_verdict` was
    lifted out of a nested closure for exactly that reason, because untestable-by-construction was
    the whole of the admission.

    Asked only where the ORIGINAL parses, so a mutated JSON fixture is never refused for having
    stopped being a program it never was.
    """
    if not _parses(original) or _parses(mutated):
        return False, ""
    return True, ("the body declared for it in MUTANTS is malformed, so the file stopped being a "
                  "program and NOTHING was exercised")


def neuter(src, fn, body):
    """Replace fn's body with `body`, keeping its signature and dropping its docstring.

    INDENT-AWARE, because the finder and the mutator disagreed about what a producer IS.
    `candidates()` walks the whole tree deliberately — a class's methods are not module level, and
    that widening was prompted by `limits_lock`'s methods sitting outside the scan — while this
    matched `^def name(` at COLUMN ZERO. So a nested function or a method could be a candidate the
    mutator could never reach: declared in MUTANTS, its anchor reports NOT FOUND on every sweep,
    forever, and a producer permanently NOT MEASURED looks decided while never being measured.
    Eighteen of 174 candidates were in that state, all of them excluded with reasons, and a gate now
    refuses declaring one as a mutant — this removes the reason that gate has to exist.

    The BODY is re-indented to the def's own level, so a caller writes it once at the natural
    four-space depth and it lands correctly whether the target is at column zero or inside a class.
    Without that, an indent-aware match would produce a file that parses as something else entirely,
    which is the failure mode this file spent a day learning to check for.
    """
    lines = src.split("\n")
    for i, l in enumerate(lines):
        m = re.match(rf"^(\s*)def {re.escape(fn)}\(", l)
        if not m:
            continue
        indent = m.group(1)
        j = i + 1
        # The body is every following line indented DEEPER than the def, plus blank lines. At
        # column zero this is the old rule exactly; deeper in it stops at the next sibling.
        while j < len(lines) and (not lines[j].strip()
                                  or (lines[j].startswith(indent)
                                      and lines[j][len(indent):len(indent) + 1] in (" ", "\t"))):
            j += 1
        raw = body.rstrip("\n").split("\n")
        base = min((len(x) - len(x.lstrip()) for x in raw if x.strip()), default=0)
        # ONE LEVEL DEEPER THAN THE DEF, not level with it. The first version prepended only the
        # def's own indent, so at module level (indent "") a body written "    return None" came
        # back "return None" and every one of the 122 existing mutants changed. Caught by diffing
        # all of them against the old function before shipping — which is the check this file spent
        # the day arguing for, run on the change to the file that argues for it.
        shifted = [indent + "    " + x[base:] if x.strip() else x for x in raw]
        return "\n".join(lines[:i + 1] + shifted + lines[j:]), True
    return src, False


# THE SECTION MAP, and the two facts that make using it honest.
#
# SOUND: a kill count is {passing at baseline} - {passing with the producer neutered}. An assertion
# in a section that never executes the producer is in BOTH sets and cancels, so running only the
# sections where a producer's killers live gives the SAME number. Measured over 96 producers: 71
# have every killer in ONE section, 17 in two, 8 in three, out of 79. The whole-suite denominator is
# preserved by arithmetic; running all of it was preserving it by brute force.
#
# STALE-SAFE: the map comes from a previous FULL sweep, so an assertion added since could kill a
# producer the map does not know about. Then the trimmed run finds FEWER kills, lands below the
# recorded floor, and FAILS — loudly, with the remedy printed. It cannot report a producer covered
# when it is not. That asymmetry is the whole licence for this mode; without it the trim would be
# buying speed with the denominator, which is what this file exists to refuse.
KILL_NAMES = {}          # key -> the COMPLETE set of assertion names a mutant flipped.

# THE COUNT THE TABLE DECLARED AT IMPORT, captured before anybody can replace it. test/run.py drives
# `sweep.main()` with THREE fake producers to assert report ordering, and both artifact writers run
# inside main() and write into the REAL test/ directory — so a fixture testing something else was
# free to overwrite a full sweep's measurements with its own three rows. The section map escaped
# only by accident (its section lookup fails for those fakes and it returns early); the killer sets
# did not, and shipped a 138-byte file with three producers and no killers in it.
#
# Comparing against len(MUTANTS) at write time cannot see this: the fixture replaces MUTANTS, so
# both numbers are 3 and agree. The count has to be taken when the module is first read, which is
# the one moment the table is certainly the module's own.
_DECLARED_PRODUCERS = len(MUTANTS)


def _is_full_sweep():
    """True only when this run measured every producer the table declared at import time.

    A trimmed or stubbed run has not observed what it did not execute. Letting one write either
    artifact ratchets it toward whatever ran last — and the failure is silent, because a smaller
    file is still a valid file and the next reader cannot tell it is looking at a sample.
    """
    return len(KILL_NAMES) == _DECLARED_PRODUCERS

                         # The printed report shows `targeted[:3]`; building the section map from
                         # THAT is building it from a truncated view, which is exactly what I did
                         # first: 40 of 102 producers came back short and the floor check failed
                         # them. The map is written from this, the tool's own structure.
FAST = os.environ.get("GAME_LOOP_SWEEP_FAST") == "1"
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "sweep-sections.json")) as _f:
        SECTION_MAP = json.load(_f)
except (OSError, ValueError):
    SECTION_MAP = {}


def run(tree, timeout=1800, sections=None):
    """The suite's stdout, or None if it did not finish in time.

    A DEADLINE, because a mutant that hangs measures nothing and waiting for it measures nothing
    either (showrunner). Before this, a timeout raised out of the worker and took the whole sweep
    with it — so one unscoreable producer cost every other producer's measurement too.
   
    `sections` runs a SUBSET, and the caller is responsible for the only thing that makes that
    sound: the baseline and the mutant must be given the SAME subset. A kill count is a set
    difference, so an assertion that runs in neither side cancels — but one that runs in only one
    side is counted as a kill or missed as one, which is not a measurement at all.
    """
    return run_detail(tree, timeout, sections)[0]


def run_detail(tree, timeout=1800, sections=None):
    """As `run`, plus the two things it used to throw away: the exit status and stderr.

    They were discarded, so a suite that produced no summary line could only ever be reported as
    the MUTANT's doing — there was nothing else left to say. See `died_how` for what that cost.
    """
    cmd = [sys.executable, "test/run.py"]
    for sec in (sections or ()):
        cmd += ["--section", sec]
    try:
        r = subprocess.run(cmd, cwd=tree, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, None, ""
    return r.stdout, r.returncode, (r.stderr or "")


def died_how(rc, err):
    """Why a run produced no summary line, as a fact about the PROCESS rather than the mutant.

    OBSERVED (INV4): twelve CONTIGUOUS producers — MUTANTS indices 109-120, which is exactly the
    worker count — reported NOT MEASURED within two seconds of each other, every one with empty
    stdout, in a sweep that overlapped other full-suite runs on the same machine. Twelve
    independent mutants do not break the suite in the same two seconds; one batch of workers dies
    together. The report nonetheless said "Fix the anchor or the crash and re-measure", twelve
    times, about twelve functions that were fine — and I spent an hour believing it, first
    blaming the sweep's concurrency and then a stray `pkill`, neither of which the log could
    confirm or refute.

    It could not say anything better: `run` kept stdout and dropped the exit status and stderr, so
    "this mutant breaks the suite" and "something killed my subprocess" arrived as the same bytes.
    A third outcome that cannot be told from the second is not a third outcome.

    A NEGATIVE returncode is the one that matters: the process did not choose to exit, so nothing
    in that tree is evidence about the producer.
    """
    if rc is None:
        return "the deadline expired before it finished"
    if rc < 0:
        return (f"KILLED BY SIGNAL {-rc} — the process did not choose to exit, so this says "
                "NOTHING about the mutant. Something on this machine ended it (memory pressure, "
                "a concurrent suite, a stray pkill). Re-run this producer alone before believing "
                "any verdict about it")
    tail = [l.strip() for l in (err or "").strip().splitlines() if l.strip()][-3:]
    if not tail:
        return f"exit {rc}, and it printed nothing on stderr either"
    return f"exit {rc}; stderr: " + " / ".join(t[:150] for t in tail)


def passing(out):
    return [m.group(1) for m in re.finditer(r"^  ok   (.*)$", out, re.M)]


def _parse_argv(argv):
    """Refuse what this script does not understand, INSTEAD OF running for an hour anyway.

    There was no argument handling at all, so every flag was silently ignored — including the one
    everybody tries first. `python3 test/mutation_sweep.py --help` did not print help: it started a
    full sweep, ~70 minutes, with no output for the first several minutes, which is
    indistinguishable from a script that has hung. Asking a tool what it does should never be the
    most expensive thing you can do to it.

    argparse is not used here on purpose: this takes no options, and adding a parser to reject
    arguments would invite the reading that some exist.
    """
    if not argv:
        return
    if any(x in ("-h", "--help") for x in argv):
        print(__doc__.strip() if __doc__ else "mutation_sweep — the full mutation sweep.")
        print()
        print("usage: python3 test/mutation_sweep.py")
        print()
        print("Takes NO ARGUMENTS. It is configured by ENVIRONMENT, and this list is the only")
        print("place that says so — `--help` saying 'one mode' is how an hour-long tool hid a")
        print("cheaper one from the person most likely to want it.")
        print()
        print("  (default)                 sweep every producer in MUTANTS against the WHOLE suite.")
        print("                            Roughly an hour on 14 cores.")
        print("  GAME_LOOP_SWEEP_JOBS=N    producers in flight at once. Default: cores - 2, cap 12.")
        print("  GAME_LOOP_SWEEP_FAST=1    scope each producer to the sections that killed it in")
        print("                            the last FULL sweep, with its baseline taken over the")
        print("                            same subset so the arithmetic stays honest.")
        print()
        print("MEASURED, not estimated — 130 producers, 12 in flight, same machine:")
        print("  throughput   full 1.8-2.0 producers/min · FAST 4.5 · steady across the whole run")
        print("  agreement    FAST reproduced the full sweep's kill count on 130 of 130 producers")
        print()
        print("IT DOES NOT MAKE THE FIRST RESULT ARRIVE SOONER, which is usually what you want when")
        print("you reach for it. The baseline is the WHOLE suite in both modes (~5 min), reports")
        print("print in strict MUTANTS order rather than completion order, and producer #1 is")
        print("unmapped — so it pays a full run and gates every other line. Expect ~10 minutes of")
        print("silence before anything appears. That is not a hang.")
        print()
        print("WHAT FAST TRADES AWAY, because a flag documented without its cost is worse than an")
        print("undocumented one: it looks only where a killer was found BEFORE, so it cannot")
        print("discover one in a section that did not kill that producer last time. Use it to")
        print("re-confirm known coverage cheaply; do NOT use it to measure a producer whose")
        print("assertions have moved. A producer with no map entry falls back to the whole suite —")
        print("correct, just not faster. With no map at all it refuses and says so.")
        print()
        print("The map (test/sweep-sections.json) is a byproduct of a FULL sweep and only of one:")
        print("a trimmed run cannot observe the sections it did not execute, so letting it rewrite")
        print("the map would ratchet it down toward whatever ran last, until it measured nothing.")
        print()
        print("It gates nothing — no commit, no verify rule waits on it — so run it detached and")
        print("read it when it lands.")
        raise SystemExit(0)
    print(f"mutation_sweep takes no arguments; got: {' '.join(argv)}", file=sys.stderr)
    print("Refusing rather than sweeping, because this run costs about an hour and an ignored",
          file=sys.stderr)
    print("flag means you asked for something this script does not do. `--help` lists the modes",
          file=sys.stderr)
    raise SystemExit(2)


def main():
    # FAST WITH NO MAP IS A SLOW RUN THAT SAYS NOTHING. `SECTION_MAP` falls back to {} on any
    # unreadable or unparseable file, and in FAST mode every `SECTION_MAP.get(key)` then returns
    # None, so every producer runs the WHOLE suite: correct results, no speedup, and an hour spent
    # by somebody who asked for minutes and was told nothing.
    #
    # This was `_write_section_map`'s declared KNOWN GAP, and the gap was never that it could not be
    # checked -- the entry says so itself: "that is the shape worth an assertion and it does not
    # have one yet". An absent map is silent BECAUSE nothing announced it, which is the same
    # absence-reads-as-normal shape this file exists to refuse. Loud now, and therefore assertable.
    if (_fastnote := fast_without_map_notice(FAST, SECTION_MAP)):
        print(_fastnote)
    base = tempfile.mkdtemp(prefix="sweep-base-")
    subprocess.run(f"git -C {REPO} archive HEAD | tar -x -C {base}", shell=True, check=True)
    found = all_candidates(base)

    # Default-deny first, and before the slow part: a producer nobody decided about is the case
    # this file exists for, and it is answerable in one line rather than in ten minutes.
    if coverage_gate(found):
        shutil.rmtree(base, ignore_errors=True)
        return 1
    files = sorted({k.split("::")[0] for k in found})
    print(f"{len(found)} candidate producers across {len(files)} source file(s): "
          f"{len(MUTANTS)} swept, {len(NOT_SWEPT)} excluded with a reason, 0 undecided.")
    print("  " + " · ".join(f"{f} ({sum(1 for k in found if k.startswith(f + '::'))})"
                            for f in files) + "\n")

    print("producer mutation sweep — assertions that SURVIVE a neutered producer")
    print(f"(the tree under test is HEAD, not the working copy; thin under {THIN_AT} kills, "
          "only ZERO fails)\n")
    # The unmutated run, so a kill is a named assertion that FLIPPED rather than a count that moved.
    baseline_out = run(base)
    if baseline_out is None:
        print("BASELINE TIMED OUT — nothing can be measured against a suite that did not finish, "
              "and every floor taken now would be a floor against a shorter run. Refusing to "
              "report.")
        shutil.rmtree(base, ignore_errors=True)
        return 1
    # WHAT THIS SET DIFFERENCE ASSUMES ABOUT ITS INPUTS — asked deliberately rather than after
    # being bitten, and every answer is now either enforced or measured:
    #
    #   names are UNIQUE          a duplicate is one set element, so a kill in one of a pair is
    #                             invisible and the producer reports thinner than it is.
    #                             ENFORCED: test/run.py asserts uniqueness over its 1358 literal
    #                             messages. There was exactly one collision when first checked.
    #   names are STABLE run to   an interpolated value that varies between runs leaves the
    #   run                       baseline set and enters the mutant set, which reads as a kill
    #                             AND a new assertion. MEASURED 2026-08-23 at 3a83c411: two runs
    #                             of the same tree, 1453 names each, zero differences either way.
    #                             40 of the 1398 messages are computed; all interpolate from
    #                             source scans, fixtures or loop variables, none from product
    #                             output. A fact about that day, not a guarantee — re-run two
    #                             copies and diff the names if a message starts embedding
    #                             something the code under test produces.
    #   the baseline FINISHED     refused below, loudly, rather than measured against a short run.
    #   the mutant FINISHED       NOT MEASURED below, distinct from UNPROTECTED and from THIN.
    #   the anchor MATCHED        neuter() reports a miss and it is not silently skipped.
    baseline = set(passing(baseline_out))
    # A FLOOR MEASURED AGAINST AN UNFINISHED SUITE IS NOT A FLOOR. Every kill count this file has
    # ever printed was measured against a baseline of 788 instead of 966, because the suite CRASHED
    # in the archived tree — no .git, so `self --pin HEAD` could not resolve and a bare open() raised
    # ~180 assertions early. Assertions that never ran cannot flip, so a producer covered only by
    # them reported UNPROTECTED: the one verdict here that fails a run, reached by not looking.
    #
    # The denominator is the part nothing reports on, which is this file's own oldest lesson arriving
    # in its own measurement. So the baseline now has to SAY it finished, and the next crash — which
    # will be somewhere else — stops the run instead of shortening it silently.
    if not re.search(r"^\d+ passed, \d+ failed", baseline_out, re.M):
        print("BASELINE DID NOT FINISH — the suite crashed or was killed in the archived tree, so "
              "every assertion after that point")
        print("is outside this sweep's denominator and any floor measured now would be a floor "
              "against a shorter suite. Refusing")
        print("to report. Reproduce with: git archive HEAD | tar -x -C <dir> && cd <dir> && "
              "python3 test/run.py")
        print(baseline_out[-1200:])
        shutil.rmtree(base, ignore_errors=True)
        return 1
    # AND SAY WHAT THE DENOMINATOR EXCLUDES. Restoring the crash was not the whole fix: assertions
    # that FAIL in the archived tree are outside it too, for the same reason — they cannot flip. They
    # fail honestly (no .git, so the git-dependent arms cannot pass), but a count nobody prints is a
    # silent cap, which is the shape this file exists to refuse.
    _trailer = re.search(r"^(\d+) passed, (\d+) failed", baseline_out, re.M)
    _failed = int(_trailer.group(2)) if _trailer else 0
    if _failed:
        print(f"note: {_failed} assertion(s) FAIL in the archived tree and are therefore outside "
              "this denominator —")
        print("      they cannot flip, so no producer can be credited or blamed for them. Not a "
              "silent cap: printed every run.")
        # AND NAMED, not just counted. The comment above has said "no .git, so the git-dependent
        # arms cannot pass" since this note was written — in a comment, where it reaches nobody
        # reading the output. A bare count cannot be told apart from seven REAL regressions, which
        # is the same two-outcomes-one-observable this file exists to refuse. Naming them is what
        # makes the reader able to judge; the count alone asks them to trust it.
        for _line in re.findall(r"^ +FAIL (.+)$", baseline_out, re.M):
            print(f"      · {_line[:110]}")
        print("      Expected shape: this tree is a `git archive` extract, so it has NO .git and "
              "every arm that")
        print("      shells out to git fails honestly. A real clone passes them. If a name here "
              "is NOT git-dependent,")
        print("      that is a genuine regression wearing this note as camouflage.")
    print(f"baseline: {len(baseline)} named assertions pass unmutated\n", flush=True)

    verdicts = [None] * len(MUTANTS)
    reports = [None] * len(MUTANTS)
    elapsed = [None] * len(MUTANTS)

    def sweep_one(i):
        """Measure ONE producer. Returns (verdict-tuple, report-text) and prints nothing.

        Independent by construction -- its own temp tree, its own suite run, no shared state -- so
        the only reason these ever ran one at a time was that the loop was written that way.
        Printing is deferred to the caller so that concurrency cannot interleave two reports into
        an unreadable one, and so the order stays the order of MUTANTS rather than of luck.
        """
        label, key, body, marks, thin_note, floor = MUTANTS[i]
        rel, fn = key.split("::", 1)
        try:
            with open(os.path.join(base, rel)) as f:
                original = f.read()
        except OSError:
            return ((key, None, NOT_MEASURED, floor),
                    f"  !! {key}: {rel} is not in the tree under test. NOT MEASURED — this is not "
                    f"a coverage finding.\n")
        mutated, hit = neuter(original, fn, body)
        if not hit:
            # Not a skip. A producer named here that no longer exists is zero evidence about
            # zero code, and a sweep that shrugs at that is a check that cannot fail.
            # NOT FOUND is NOT UNPROTECTED. Reporting the fatal verdict here is a confident
            # claim about code that was never mutated — and the caveat on this line was being
            # discarded by the summary, which is where anyone actually believes a number.
            return ((key, None, NOT_MEASURED, floor),
                    f"  !! {key}: NOT FOUND in {rel} — renamed, or gone. NOT MEASURED: nothing was "
                    f"mutated, so this says nothing about coverage either way.\n")
        # A MUTANT THAT NEVER PARSED DID NOT RUN AT ALL (#117), and until now it arrived at the
        # same sentence as a mutant that took the suite down: "the suite did not finish". Same
        # words, opposite repairs. A crash means the mutation was too broad and the producer may
        # be fine; a parse failure means the DECLARED BODY in this file is malformed and no code
        # was ever exercised. Nine producers sat NOT MEASURED across two sweeps for exactly that —
        # a literal backslash-n in the body — and the message never once pointed at the entry.
        _unparseable, _why = declared_mutant_unparseable(original, mutated)
        if _unparseable:
            return ((key, None, NOT_MEASURED, floor),
                    f"  !! {key}: THE DECLARED MUTANT DOES NOT PARSE — {_why}.\n"
                    f"     NOT MEASURED, and it is a defect in THIS file rather than a coverage\n"
                    f"     finding: fix the body. A mutant that CRASHES the suite is a different\n"
                    f"     report; this one never ran at all.\n")
        if mutated == original:
            # THE OTHER HALF OF "NEVER APPLIED", and NOT_MEASURED's own header already claimed it.
            # `neuter` reports `hit` when it FINDS the function, not when the edit changes anything,
            # so a body that already equals its neutered form produced a byte-identical file, a
            # clean suite, and the verdict SURVIVED. That does not read as a broken control — it
            # reads as a COVERAGE GAP, and sends the next reader hunting for an assertion that
            # already exists. The wasted work is downstream of the wasted control and is
            # indistinguishable from real work.
            #
            # Reported by a peer who found 44 of their 54 mutant-builders unverified: a `sed` that
            # matches nothing exits 0, a `.replace()` that matches nothing returns the original.
            # MEASURED HERE BEFORE ADDING THIS: all 108 declared producers mutate for real today, 0
            # no-ops and 0 missing anchors — so this is not a fix for a live defect, it closes the
            # gap between what the NOT_MEASURED contract SAYS it covers and what it checked.
            return ((key, None, NOT_MEASURED, floor),
                    f"  !! {key}: the mutation APPLIED BUT CHANGED NOTHING — {fn} in {rel} already "
                    f"reads exactly like its neutered form, so the tree under test is byte-identical "
                    f"to the baseline. NOT MEASURED: a clean suite here would mean nothing was "
                    f"broken, not that nothing catches it.\n")
        t = tempfile.mkdtemp(prefix="sweep-")
        try:
            shutil.copytree(base, t, dirs_exist_ok=True)
            with open(os.path.join(t, rel), "w") as f:
                f.write(mutated)
            os.chmod(os.path.join(t, rel), 0o755)
            # THE SAME SUBSET ON BOTH SIDES, or the difference is not a measurement. In fast
            # mode this producer gets its OWN baseline over its own sections — the shared
            # whole-suite baseline would be a set of 1719 minus a set of 40, i.e. every assertion
            # that simply did not run, reported as a kill.
            _secs = SECTION_MAP.get(key) if FAST else None
            if _secs:
                _b = run(base, sections=_secs)
                if _b is None:
                    return ((key, None, NOT_MEASURED, floor),
                            f"{label}\n  NOT MEASURED — the SUBSET baseline timed out.\n")
                local_base = set(passing(_b))
            else:
                local_base = baseline
            out, _rc, _err = run_detail(t, sections=_secs)
            if out is None:
                return ((key, None, NOT_MEASURED, floor),
                        f"{label}\n  NOT MEASURED — the suite TIMED OUT under this mutant. A run\n"
                        f"  that hung produced no verdict, and waiting longer produces none either.\n")
        finally:
            shutil.rmtree(t, ignore_errors=True)
        still = set(passing(out))
        tail = out.strip().split("\n")[-1]
        # DID THE MUTATED SUITE FINISH? If it died early, its unrun assertions never printed `ok`,
        # so the set difference counts them all as KILLED and the producer reports strong coverage
        # measured on a run that stopped. The baseline already had to prove it finished; the mutants
        # never did, which is the same lesson one level in.
        if not re.search(r"^\d+ passed, \d+ failed", out, re.M):
            return ((key, None, NOT_MEASURED, floor),
                    f"{label}\n  suite: {tail}\n"
                    f"  NOT MEASURED — the suite did not finish under this mutant, so its unrun\n"
                    f"  assertions never printed and would have counted as KILLED. That is an\n"
                    f"  inflated number about a run that stopped, not a coverage reading.\n"
                    f"  HOW IT DIED: {died_how(_rc, _err)}.\n")
        killed = len(local_base - still)
        v = verdict(killed)
        # ASKED ONLY AT ZERO, because one kill already proves the producer executes — and a probe
        # run costs another full suite. At zero the number cannot distinguish "nothing asserts it"
        # from "nothing runs it", and those take opposite repairs.
        # WHAT THIS HAS NOT DONE (INV6), narrowed since the first version said it: the probe's five
        # outcomes AND the verdict they map to are both driven in test/run.py now — `probed_verdict`
        # was lifted out of a nested closure for exactly that reason, because untestable-by-
        # construction was the whole of the admission. What remains unexercised is only this line:
        # no producer here kills zero, which is the same clean sweep that makes the branch
        # unreachable. Confirm a first real INERT reading by hand with `mutate --prove` on the same
        # function before acting on it.
        live_why = ""
        if killed == 0:
            v, live_why = probed_verdict(original, fn, in_a_copy_of(base, rel, run))
        drift = "  ↓ BELOW FLOOR" if killed < floor else ""
        lines = [f"{label}", f"  suite: {tail}",
                 f"  killed: {killed}   [{v}]   floor {floor}{drift}"]
        if live_why:
            lines.append(f"  probe : {live_why}")
        # THE NOTE WAS MISLABELLED, NOT STALE — and I nearly deleted ten of them finding that out.
        # `external_claims_report` printed "why it is thin: 2 kills, and both are the arms written
        # for it" directly under `killed: 25`, which reads as prose contradicting its own number.
        # My first fix was to treat every such note as expired and clear it. Then I READ the other
        # ten: they are arm inventories — which pairs exist, which are absence arms, what a control
        # covers — written under a header that only makes sense while the entry is thin. The
        # content was right and the LABEL had gone wrong, so deleting them would have destroyed the
        # most specific thing each entry knows about itself to fix a header.
        #
        # A record that looks stale is sometimes a record that is mislabelled. Read it before
        # believing the first diagnosis, especially when the first diagnosis is "delete".
        _nl = note_line(v, thin_note)
        if _nl:
            lines.append(_nl)
        # A KILL NEEDS A NAMED KILLER, AND THE NAME HAS TO BE ABOUT YOUR GUARD (lamp-owner,
        # 2026-08-23). A mutation that breaks something ELSE reddens the suite and reports the same
        # word as one the guard actually caught — collateral and genuine wear the identical verdict
        # and are not distinguishable from a count. Measured here on `external_claims_report`:
        # 25 kills, of which 4 were the glyph tripwire and the orphan scan, both of which read the
        # BINARY'S SHAPE and therefore redden for any producer that gets neutered at all.
        #
        # `marks` already existed to pick out SURVIVORS worth reading. The same words identify a
        # killer that is about this producer, so the report now says how many of the kills were.
        killed_names, targeted = killers(local_base, still, marks)
        KILL_NAMES[key] = sorted(killed_names)
        if killed:
            lines.append(f"  of those {killed}, at least {len(targeted)} name this producer's "
                         f"subject ({', '.join(marks)}) — a LOWER BOUND, see below")
            lines += [f"    KILLED BY: {t[:88]}" for t in targeted[:3]]
        # THE TARGETED COUNT IS A LOWER BOUND, NOT A MEASURE, and the reason is structural: `marks`
        # was written for ONE job — picking out the SURVIVORS worth reading, where a handful of
        # relevant words is enough — and I repurposed it for a second, deciding whether a KILL is
        # about this producer. The second job needs the full vocabulary the tests actually use.
        #
        # Measured, not assumed: `_limitgate_verdict` reported 10 killed / 1 targeted, and hand-
        # neutering it showed ALL TEN are about the limit gate — "gate denies ordinary work over
        # the threshold", "the AUTO handoff does NOT satisfy the limit gate", "removing the handoff
        # closes it again". Nine of them simply do not contain the words `limit gate`, `limit` or
        # `window`. Nine producers sit at targeted 1 and that list is a VOCABULARY artefact, not a
        # coverage finding — chasing it by adding whichever words appear in today's failures would
        # fit the marks to one run and make the number rise while nothing improved.
        #
        # EVERY KILL COLLATERAL is a finding wearing a healthy number: the count says the suite
        # noticed something, and nothing in it was about this producer. Reported, not fatal — a
        # producer can be genuinely covered by assertions whose names do not carry its marks, and
        # a gate that cannot tell those apart would fail honest entries.
        if killed and not targeted:
            lines.append("  ⚠ EVERY KILL WAS COLLATERAL — nothing whose name mentions this "
                         "producer's subject failed. The number says the suite noticed a change, "
                         "not that anything here is checked. Read the marks, or the assertions.")
        lines += [f"    SURVIVED: {c[:96]}"
                  for c in sorted(x for x in still
                                  if any(m.lower() in x.lower() for m in marks))]
        return (key, killed, v, floor), "\n".join(lines) + "\n"

    # ONE FULL SUITE PER PRODUCER IS THE COST, AND IT WAS BEING PAID SERIALLY (#59). Measured at
    # ~2.5 min each against a 730-assertion suite, 41 producers came to ~1.8 hours -- and a check
    # that long is one nobody finishes. I started it three times in a day and completed it none of
    # them, and two of the abandoned runs left a collapsed baseline whose "killed: 0" reads exactly
    # like UNPROTECTED. Slow did not make it unreliable on its own; it made it unfinished, which
    # amounts to the same silence.
    #
    # Deliberately NOT solved by sweeping fewer producers or by running a subset of the suite per
    # producer: the first buys speed with the denominator this file exists to defend (#44), and the
    # second changes what the number MEANS, since a kill count is a set difference over the whole
    # suite. Doing the same work at once is the only one of the three that costs nothing.
    # THE CAP WAS 6 ON A 14-CORE MACHINE, and 6 was leaving half the box idle. Measured 2026-08-25
    # by running N full suites at once and timing them, because the question is THROUGHPUT (producers
    # per minute) and not latency (how long one suite takes):
    #
    #      6 concurrent full suites   wall 302s   per-run avg 298s   1.19 runs/min
    #     12 concurrent full suites   wall 396s   per-run avg 390s   1.82 runs/min
    #
    # Six runs cost what ONE costs — there is almost no contention at that width, which is what
    # `min(6, cpu//2)` was protecting against. Twelve makes each run 31% slower and the FLEET 53%
    # faster, and a sweep is bound by the fleet: 102 producers goes from ~86 min to ~56.
    #
    # Still capped, and still `cpu - 2`: the suite spawns subprocesses of its own, so leaving two
    # cores means the machine stays usable while an hour-long sweep runs — and an unusable machine
    # is how a sweep gets abandoned, which is the failure the comment above is about.
    jobs = int(os.environ.get("GAME_LOOP_SWEEP_JOBS") or 0) or max(1, min(12, (os.cpu_count() or 2) - 2))
    print(f"running {len(MUTANTS)} producers, {jobs} at a time "
          f"(GAME_LOOP_SWEEP_JOBS to change; each is one full suite run)\n", flush=True)

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {}
        for i in range(len(MUTANTS)):
            futures[pool.submit(sweep_one, i)] = (i, time.monotonic())
        nxt = 0
        for fut in concurrent.futures.as_completed(futures):
            i, t0 = futures[fut]
            elapsed[i] = time.monotonic() - t0
            verdicts[i], reports[i] = fut.result()
            # Emit in MUTANTS order as each prefix becomes ready: the report stays deterministic
            # AND a redirected run still shows progress rather than 40 silent minutes.
            while nxt < len(MUTANTS) and reports[nxt] is not None:
                # LABELLED "at", because the bare number was read as this producer's DURATION and a
                # wrong throughput figure was published off it. It is elapsed SINCE THE RUN STARTED,
                # and the column looks unsorted only because these print in MUTANTS order rather
                # than completion order — so "duration" is exactly the reading the format invited.
                print(reports[nxt] + f"  (at {elapsed[nxt]:.0f}s)\n", flush=True)
                nxt += 1
    # BEFORE THE ARCHIVE IS REMOVED. The first version called this after the summary, 120 lines
    # past `shutil.rmtree(base)` — so it opened a tree that no longer existed, hit OSError, and
    # returned SILENTLY. A full sweep ran for 50 minutes and wrote no map, and nothing said so.
    if not FAST:
        _write_section_map(base)
        _write_killers()
    shutil.rmtree(base, ignore_errors=True)

    # RECORDED, so this argument is never had from memory again -- #59 was filed on a figure I had
    # to derive by hand from a part-finished run.
    total = time.monotonic() - started
    timed = [(elapsed[i], MUTANTS[i][1]) for i in range(len(MUTANTS)) if elapsed[i] is not None]
    if timed:
        worst = sorted(timed, reverse=True)[:3]
        print(f"producer time: {total / 60:.1f} min wall for {sum(t for t, _ in timed) / 60:.1f} "
              f"min of work at {jobs} at a time · slowest: "
              + " · ".join(f"{k.split('::')[-1]} {t:.0f}s" for t, k in worst) + "\n")
    verdicts = [v for v in verdicts if v is not None]

    thin = [f"{fn} ({k})" for fn, k, v, _ in verdicts if v == THIN]
    bad = [fn for fn, _, v, _ in verdicts if v == UNPROTECTED]
    # INERT IS ITS OWN GROUP, never folded into UNPROTECTED — the whole point of measuring it is
    # that the repair differs. Fatal either way, but "write an assertion" is the wrong instruction
    # for a producer nothing calls, and a fatal line that names the wrong fix gets one applied.
    inert = [fn for fn, _, v, _ in verdicts if v == INERT]
    # UNSCOREABLE, kept apart from every other list here on purpose.
    unscored = [fn for fn, _, v, _ in verdicts if v == NOT_MEASURED]
    # A recorded floor that is not checked is prose. THIN is a standing acceptable state, so it
    # reports; DRIFT is never that — it means coverage that existed has been lost, which is the
    # exact regression this tool exists to catch, so it fails. The remedy is not to edit the number
    # quietly: re-record it WITH the reason, the way ruled_out's thinness carries its own.
    drifted = [f"{fn} ({k} < {fl})" for fn, k, v, fl in verdicts
               if k is not None and v != NOT_MEASURED and k < fl]
    # KNOWN GAPS are declared, not silent. NOT_SWEPT holds two kinds of entry — a genuine exclusion
    # and a DEBT — and printing only the first kind turns the debt into a denylist with extra steps:
    # decided once, then never read again. The run says how much is owed, every time.
    gaps = sorted(k for k, why in NOT_SWEPT.items() if (why or "").strip().startswith("KNOWN GAP"))
    if gaps:
        print(f"KNOWN GAPS — declared, NOT swept ({len(gaps)}): " + " · ".join(gaps))
        print("Each should be swept and is not yet, with its reason recorded beside it.")
    if thin:
        print("THIN — reported, not fatal: " + " · ".join(thin))
    # A FLOOR SO FAR BELOW THE MEASUREMENT THAT IT WOULD NOT NOTICE A COLLAPSE. The drift check
    # above catches coverage going DOWN past the floor; nothing catches the floor itself going
    # stale, and nothing ever raises one. Measured 2026-08-25 at 8172f90: 58 OF 98 producers score
    # above their recorded floor, and `upstream_check` records 0 while killing 15 — so its tripwire
    # would sit silent through a total loss of coverage. The recorded number stops being a tripwire
    # and becomes a souvenir of the day somebody measured it.
    #
    # THOSE FIGURES ARE THE CORRECTED ONES, and how they were wrong is the reason this comment says
    # so. The first pass reported 49 of 86, from a regex hand-rolled over this report's RENDERED
    # OUTPUT — and `^([a-z_][a-z0-9_]*) ->` silently dropped every dotted producer, so the twelve in
    # bin/verify, bin/watchdog and bin/notify.py (verify.owed, watchdog.claim_pidfile, notify.send …)
    # were never in the denominator. A short denominator, measured by improvising an instrument for a
    # measurement this file already performs, in the file whose oldest lesson is the short
    # denominator. The function below takes `verdicts` — the tool's own structure — for that reason.
    #
    # THE BOUND IS THE HALVING, not a round number: reported when the floor would permit losing MORE
    # THAN HALF of what is measured today. That is a statement about the blind spot rather than
    # about drift, it explains itself in the line, and it does not fire on the ±1 churn that normal
    # assertion edits produce. NOT FATAL — a conservative floor has never broken anything, and a run
    # that fails on good news teaches people to raise floors without measuring them.
    wide = []
    for _lbl, _key, _b, _marks, _n, _fl in MUTANTS:
        _m, _bad = overbroad_marks(_marks, baseline, _fl)
        if _bad:
            wide.append((_m, _fl, _key.split("::")[-1]))
    # THE OTHER DIRECTION, AND IT IS THE WORSE ONE. Everything above measures how WIDE a mark set
    # is. Nothing measured whether it matches the producer's OWN KILLERS — and `killers()` uses
    # these marks for exactly that, to separate a genuine kill from collateral. A set that misses
    # its own killers under-reports the producer's coverage while looking perfectly narrow.
    #
    # Measured on this repo when the check was written: 90 of 107 producers with both marks and
    # killers matched FEWER killers than they have. `_git_sha`'s marks matched 0 of its 2.
    # `pinned_report`'s matched 4 of 204. Those cannot be found by a breadth report, because they
    # are not broad — `_git_sha` matches ONE name in the whole suite, which reads as beautifully
    # precise and is in fact precisely wrong.
    #
    # A LOWER BOUND IS STILL HONEST — the per-producer line says so — but a lower bound of 4 out of
    # 204 carries no information, and nothing in this report distinguished that from a producer
    # genuinely covered by four assertions. So the one-sided measurement is the finding: this file's
    # own advisory measured breadth and was silent about accuracy, which is the shape it exists to
    # refuse. NOT FATAL, for the same reason breadth is not: a mark set has never broken a run.
    blind = []
    for _lbl, _key, _b, _marks, _n, _fl in MUTANTS:
        _ks = KILL_NAMES.get(_key) or []
        if not _ks or not _marks:
            continue
        _hit, _tot = marks_missing_killers(_marks, _ks)
        if _hit < _tot:
            blind.append((_tot - _hit, _hit, _tot, _key.split("::")[-1]))
    if blind:
        blind.sort(reverse=True)
        # THE COUNT IS NOT THE DISTRIBUTION, and I read my own headline as a regression because of
        # it. This number counts producers missing AT LEAST ONE killer, so "names 21 of 22" and
        # "names 0 of 22" are the same row. After narrowing 45 sets by measurement the count ROSE
        # (44 -> 119, against a freshly measured killer set), while the sets I narrowed went from
        # naming 36% of their killers to 80%. The count moved one way and the coverage the other.
        #
        # So the headline carries the aggregate. A count alone invites exactly the reading I gave
        # it, in the file whose oldest lesson is that a sum is not a distribution.
        _bl_named = sum(h for _d, h, _t, _k in blind)
        _bl_total = sum(t for _d, _h, t, _k in blind)
        print(f"MARKS THAT MISS THEIR OWN KILLERS ({len(blind)} producers, naming "
              f"{_bl_named}/{_bl_total} of their killers) — the mark set does not match "
              "every assertion")
        print("  that actually killed the producer, so its 'name this producer's subject' count is "
              "an\n  UNDER-report and the gap is invisible to the breadth check above:")
        print("  " + " · ".join(f"{k} ({h}/{t})" for _d, h, t, k in blind[:10])
              + (" · …" if len(blind) > 10 else ""))
        print("  This is the other direction from breadth and it does not correlate with it — a set")
        print("  matching ONE name in the whole suite reads as precise and can still match none of")
        print("  the killers. The names to narrow TOWARD are in test/sweep-killers.json.")
        print("  NOT FATAL, same as breadth: a mark set has never broken a run. What it costs is "
              "the\n  per-producer 'at least N name its subject' line, which is a lower bound "
              "either way —\n  but a lower bound of 4 out of 204 is not a bound anybody can use.")
    if wide:
        wide.sort(reverse=True)
        print(f"MARKS TOO BROAD TO DISCRIMINATE ({len(wide)}) — these match far more assertion "
              "names than they record coverage for:")
        print("  " + " · ".join(f"{k} ({m} matched, floor {f})" for m, f, k in wide[:10])
              + (" · …" if len(wide) > 10 else ""))
        print("  `killers()` uses these to tell a GENUINE kill from collateral. A set this wide")
        print("  answers yes for almost anything, so the distinction stops discriminating without")
        print("  ever failing. Narrow them to phrases that name the producer's subject — a common")
        print("  word matched as a substring is the usual cause. Not fatal: a broad mark has never")
        print("  broken a run, it has only ever made a report agree with you.")
        print("  The names to narrow AGAINST are in test/sweep-killers.json, written by this run:"
              "\n  every assertion that actually killed each producer, not the three shown above.")
        # WHAT THIS CANNOT TELL YOU (INV6), and it took trying to act on it to find out: breadth
        # alone does not separate a LOOSE MARK from a genuinely CENTRAL PRODUCER. `fix_warning`'s
        # nine killers all name fixes and proofs, so a subject-naming set narrows it from 103
        # matches to 19 with every killer kept. `claim_pidfile`'s twenty-two span the watchdog's
        # whole behaviour -- "a NEW mandate re-arms the engine" names no pidfile at all -- so any
        # mark narrow enough to describe its subject would DROP real killers. Same advisory, and
        # the right answer is opposite.
        #
        # No signal here computes that difference. A diversity measure over the killer names was
        # tried and reported the two as identical, which is why this says so rather than shipping
        # a number that looks like it decided.
        print("  IT MEASURES BREADTH ONLY. A wide mark set is sometimes a loose mark and sometimes")
        print("  a producer the whole suite leans on, and nothing here tells the two apart — read")
        print("  the killer names before narrowing: if they share a subject, narrow to it; if they")
        print("  span the tool, the breadth is the coverage and the mark is already right.")
    slack = stale_low_floors(verdicts)
    if slack:
        print(f"FLOOR IS STALE-LOW ({len(slack)}) — measured well above what is recorded, so the "
              "tripwire has slack:")
        print("  " + " · ".join(f"{fn} ({fl}→{k})" for fl, k, fn in slack[:12])
              + (" · …" if len(slack) > 12 else ""))
        print("  Each of these would pass a run that lost more than half its coverage. Raising a")
        print("  floor is recording a MEASUREMENT, not tightening a screw: take the number from a")
        print("  full sweep of the tree it describes, the same rule the drift check asks for in the")
        print("  other direction. Not fatal, because a low floor has never broken a run — it has")
        print("  only ever failed to catch one.")
    if drifted:
        print("BELOW THE RECORDED FLOOR: " + " · ".join(drifted))
        print("Coverage that existed is gone — OR the floor is not comparable. CHECK THAT FIRST:")
        print("  a floor is only comparable to one measured against the SAME ASSERTION SET. Edit the")
        print("  assertions that cover a producer and every earlier floor becomes a number from a")
        print("  different experiment, which lands here reading exactly like coverage disappearing.")
        print("  The tell is the MUTATED run passing MORE than it used to, not fewer: this line was")
        print("  produced by two floors dropping by 2 each while their mutated runs gained 2, an")
        print("  hour after their pinning assertion was rewritten. Same code, different instrument.")
        print("If the drop IS real — assertions consolidated, a producer genuinely simplified —")
        print("re-record the floor WITH the reason. Do not just lower the number: an unexplained")
        print("floor is the target-making this file warns about. And do not re-record a floor from a")
        print("run whose assertions moved since the last one; hold the tests still and measure once.")
    if bad:
        print("UNPROTECTED — neutering these killed NOTHING: " + " · ".join(bad))
        print("Nothing in the suite notices when they stop working. That is the failure.")
        print("Each was PROBED and found live, so the code does run: what is missing is an")
        print("assertion, not a test that reaches it.")
    if inert:
        print("INERT — a `raise` in these bodies did not redden the suite: " + " · ".join(inert))
        print("Do NOT write an assertion for these yet. Either nothing calls them, or the only")
        print("callers are somewhere no assertion can see fail — a subprocess whose stderr nobody")
        print("reads. Both are different bugs from 'unasserted', and both are worse: an assertion")
        print("added here would pass forever without ever executing the code it names.")
    # UNSCOREABLE IS NOT THIN, AND IT IS NOT CLEAN EITHER. Its own group, because the whole defect
    # was a per-item caveat being flattened into an aggregate with no room for it.
    if unscored:
        print("NOT MEASURED — no reading was produced for these: " + " · ".join(unscored))
        print("This is NOT a coverage finding in either direction. A mutant that was never applied,")
        print("or whose suite crashed or hung, says nothing about whether anything notices it. The")
        print("numbers such runs used to produce were INFLATED, because an assertion that never ran")
        print("never printed `ok` and so counted as killed.")
        print("Fix the anchor or the crash and re-measure. Until then these sit outside the")
        print("denominator, which is the failure this file exists for.")
    if bad or drifted or unscored or inert:
        return 1
    print("no producer is unprotected, and none is below its recorded floor.")
    return 0


if __name__ == "__main__":
    # ARGV BELONGS TO THE ENTRY POINT, NOT TO main(). Putting this inside main() made the sweep
    # judge somebody ELSE'S command line: test/run.py drives main() programmatically, so a subset
    # run like `--section 'producer mutation sweep'` reached this parser as unrecognised flags and
    # refused, taking the whole suite down with exit 2 and 76 passing checks above it.
    #
    # Which is #110's defect wearing different clothes, committed the same day I fixed it: a check
    # whose SUBJECT is a command string it did not own. The lesson generalises past shell quoting.
    _parse_argv(sys.argv[1:])
    sys.exit(main())
