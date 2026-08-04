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

WHAT THIS DOES NOT CATCH (INV6). It measures whether an assertion NOTICES a producer that has
stopped producing. It says nothing about whether the producer is RIGHT: a wrong message and a
correct one are killed identically, because both are non-empty. It cannot see a producer whose
broken form is not the neutered one written here — a validator that wrongly ACCEPTS, a detector
that fires on everything. And a high count is not coverage: ten assertions against one line of
output kill together and count ten.

The DISCOVERY has its own edge, and it is the same kind: it reads the SHAPE of a return, never the
meaning of one. A producer that signals nothing-found with an empty string, a zero, an empty dict
or a sentinel object is not a candidate — and unlike a producer nobody listed, it will not be
missed loudly, it will simply never be enumerated. Default-deny over the shapes named here is
strictly better than a hand list; it is not the same thing as complete.
"""
import ast
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
    ("unpushed_warning -> never warns", ".game_loop/bin/game_loop::unpushed_warning", "    return None\n",
     ["unpushed", "upstream", "quiet"], None, 7),
    ("fix_warning -> never warns", ".game_loop/bin/game_loop::fix_warning", "    return None\n",
     ["fix", "quiet", "silence"], None, 9),
    ("category_tell -> never detects", ".game_loop/bin/game_loop::category_tell", "    return None\n",
     ["nudge", "category", "scope"], None, 4),
    ("aggregate_tell -> never detects", ".game_loop/bin/game_loop::aggregate_tell", "    return None\n",
     ["nudge", "aggregate", "sum"], None, 7),
    ("dominance -> never finds an outlier", ".game_loop/bin/game_loop::dominance", "    return None\n",
     ["dominan", "distribution", "spread", "event"], None, 10),
    ("ruled_out -> finds no refutations", ".game_loop/bin/game_loop::ruled_out", "    return []\n",
     ["ruled", "refut"],
     # KNOWN GAP, not a decision. Its survivors all belong to the WRITE side (`--outcome refuted`
     # refusing prose evidence, the log entry) which is separately and well covered; ruled_out() is
     # the READ side, and exactly one assertion — status reprinting the standing list — notices when
     # it returns nothing. #42 scoped itself to the four nudge/warning producers and did not touch
     # this. Fixing it means asserting a later session INHERITS the list, not deleting this note.
     "the read side of the refutation path; #42 scoped itself elsewhere and left it unfixed", 1),
    # The four the hand-written list had never been pointed at. Each was found weak by somebody who
    # was looking at something else, which is the denylist argument stated as history rather than as
    # a principle — they are here because the enumeration named them, not because anyone suspected
    # them. Their floors were MEASURED against this HEAD, not chosen.
    ("hooks_live_warning -> never warns", ".game_loop/bin/game_loop::hooks_live_warning", "    return None\n",
     ["hook", "probe", "live", "wired"], None, 6),
    ("config_paths_report -> reports no keyed path", ".game_loop/bin/game_loop::config_paths_report", "    return []\n",
     ["config path", "tracked", "write root", "tilde", "read_roots"], None, 12),
    ("worktree_report -> prints no worktree block", ".game_loop/bin/game_loop::worktree_report", "    return []\n",
     ["worktree", "drift", "rules"],
     # KNOWN GAP. #30's coverage went almost entirely to `worktree --porcelain`, which reads
     # worktree_drift() — a DIFFERENT producer — so the STATUS block this renders is asserted twice
     # and the rest of it by nothing: "✓ RULES MATCH", the no-parent-harness warning, the UNREADABLE
     # line and the "NOT compared" reach statement can all vanish unnoticed. Fixing it means
     # asserting the matched and unreadable arms of the block, not deleting this note.
     "coverage went to `worktree --porcelain` (a different producer); the status block has two", 2),
    ("update_notice -> never announces an update", ".game_loop/bin/game_loop::update_notice", "    return None\n",
     ["update", "newer", "sha", "version"],
     # Thin, and defensibly so — but say which. Its two silences ("update_check:false silences the
     # notice", "no VERSION → silent") are real restraint assertions and they DO have a companion
     # that dies. What is thin is the other side: one assertion carries the entire message, so its
     # content — both shas, the re-install command — rests on a single string match.
     "its silences are properly paired; the MESSAGE rests on one assertion, and that is the gap", 1),
    ("limits_inert_warning -> never announces that the limit gates are inert",
     ".game_loop/bin/game_loop::limits_inert_warning", "    return None\n",
     ["limit", "inert", "snapshot", "tap"],
     # Measured at 6 against a baseline of 480. The paired negative — "a real window silences the
     # warning entirely" — correctly does NOT flip here: neutering to `return None` makes the
     # producer silent everywhere, which is the arm that assertion already permits. That is the
     # pairing behaving as designed rather than a hole, and it is why the count is 6 and not 7.
     None, 6),
    ("fire_triggers -> a project's attachments never run", ".game_loop/bin/game_loop::fire_triggers", "    return []\n",
     ["trigger", "attach", "harden", "stepback"], None, 9),
    ("triggers_report -> status never mentions an attachment", ".game_loop/bin/game_loop::triggers_report",
     "    return []\n", ["trigger", "attach", "never fired"],
     # Measured at 2 when first written, and that was the whole warning: the two assertions were the
     # never-fired pair, so the THIRD state — fires every time and fails every time — was invisible,
     # which is the one a never-fired warning is silent about. Adding it took this to 4.
     None, 4),
    ("retro_outcome -> a retro never reports what the last one yielded", ".game_loop/bin/game_loop::retro_outcome",
     "    return []\n", ["retro", "yield", "harden"],
     # Also 2 at first, both about hardens. A chapter can be all evidence and no encoding, and the
     # ledger has to show that SHAPE rather than one number, so the claims/triggers arm was added.
     None, 3),
    ("working_tree_report -> never says you are in a different tree", ".game_loop/bin/game_loop::working_tree_report",
     "    return []\n", ["worktree", "tree", "harness answers"], None, 3),
    # THE TWO #44 SURFACED THAT MATTER, now measured rather than described. Both were entirely
    # OUTSIDE the denominator until the file list came from git — not excluded, absent.
    ("verify.owed -> nothing ever owes a check", ".game_loop/bin/verify::owed",
     "    return None\n", ["verify", "owes", "stale", "commit"],
     # 23 against a baseline of 534, the highest in this file, and it should be: an always-empty
     # return means `verify --check` reports clean and the commit gate passes EVERYTHING -- #25's
     # failure verbatim, in the function that decides it.
     None, 23),
    ("watchdog.exhausted_windows -> no usage window is ever exhausted",
     ".game_loop/bin/watchdog::exhausted_windows", "    return None\n",
     ["watchdog", "park", "limit", "exhaust"],
     # 4. Neutered, the run never parks and never rings itself awake at the reset -- which is
     # exactly the live state #45 found, so the mutation and the real defect are the same thing.
     None, 4),
    # The rest of #44's ten, measured against a baseline of 534 once the file list came from git.
    # Eight of the ten turned out to be genuinely well protected; the surprise was how well, which
    # is worth saying because the issue's framing (and mine) assumed the opposite.
    ("verify.changed_files -> no file ever looks changed", ".game_loop/bin/verify::changed_files",
     "    return None\n", ["verify", "changed", "stale", "owes"], None, 55),
    ("verify.staged_files -> nothing is ever staged", ".game_loop/bin/verify::staged_files",
     "    return []\n", ["staged", "blast", "commit"], None, 5),
    ("notify.send -> a page is never actually sent", ".game_loop/bin/notify.py::send",
     "    return None\n", ["notify", "slack", "page", "send"], None, 11),
    ("watchdog.claim_pidfile -> the watchdog can never claim the pidfile",
     ".game_loop/bin/watchdog::claim_pidfile", "    return False\n",
     ["watchdog", "pidfile", "quiet", "ring"], None, 9),
    ("watchdog.limits_snapshot -> the watchdog never sees a usage snapshot",
     ".game_loop/bin/watchdog::limits_snapshot", "    return None\n",
     ["watchdog", "limit", "park", "snapshot"], None, 4),
    ("watchdog.transcript_size -> idleness becomes unmeasurable",
     ".game_loop/bin/watchdog::transcript_size", "    return None\n",
     ["watchdog", "idle", "transcript", "ring"],
     # THIN at 1, and the shape is familiar: one assertion carries the whole producer. Neutered, the
     # watchdog cannot tell a parked run from a working one -- which is the entire premise of the
     # autonomy engine -- and exactly one named assertion notices.
     "one assertion carries it; the watchdog's own idleness measurement deserves a companion", 1),
    # #37's two, measured at 2 each against a baseline of 543.
    ("behaviour_changes -> the update notice never has anything to report",
     ".game_loop/bin/game_loop::behaviour_changes", "    return []\n",
     ["behaviour", "update", "changed", "cost"],
     # THIN at 2, and honestly so: the parser is exercised through the notice rather than directly,
     # so its ORDERING and its tolerance of a malformed record rest on the same two assertions that
     # cover the notice itself. A companion asserting seq order on a scrambled record is what is owed.
     "exercised only through the notice; ordering and malformed-input tolerance share its assertions",
     2),
    ("_remote_behaviour -> the record on main can never be fetched",
     ".game_loop/bin/game_loop::_remote_behaviour", "    return None\n",
     ["behaviour", "update", "fetch", "unreachable"],
     # THIN at 2 by construction: an unreachable record is DESIGNED to be indistinguishable from a
     # quiet one in everything except that it must not claim 'nothing changed'. There is little
     # surface to assert beyond that, which is the point rather than a gap.
     "an unreachable record is meant to be quiet; only its refusal to claim 'nothing changed' shows",
     2),
    ("_compare_versions -> the update check can never tell ahead from behind",
     ".game_loop/bin/game_loop::_compare_versions", "    return None\n",
     ["update", "ahead", "behind", "ancestry", "determined"],
     # 7 against a baseline of 560. Neutered, every comparison degrades to "could not determine",
     # which is the honest fallback -- so what dies is the ability to tell the three answers apart,
     # which is exactly what #49 was about.
     None, 7),
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
NOT_SWEPT = {
    # --- EXCLUDED: pure git helpers. Their None means "git failed / no such ref / no repo", which
    # is a mechanical outcome, not a verdict about the project. Each is swept through the producer
    # that calls it — the config-paths block already asserts BOTH arms in one observation ("a
    # failing git degrades to silence" beside "the same config on a working git DOES warn").
    ".game_loop/bin/game_loop::_git": "pure git helper — None means git failed, not a finding withheld; swept through its "
            "callers (unpushed_warning, config_paths_report, main_checkout), which assert the "
            "git-failed arm beside the git-worked one",
    ".game_loop/bin/game_loop::_git_out": "pure git helper for an arbitrary tree — None is 'no such ref / not a repo', a "
                "mechanical outcome. Its callers are attribution_tree, merge_files and "
                "cmd_attribute, which turn every one of those Nones into a STATED refusal that "
                "`game_loop attribute` is asserted on; none of them can report by silence",
    ".game_loop/bin/game_loop::_git_sha": "pure git helper — None is 'no HEAD here'. Its two callers are running_version and "
                "pinned_report, and pinned_report's own entry below is the honest one to read: "
                "sweeping THAT would sweep this, and it has not been done yet",
    ".game_loop/bin/game_loop::_rev": "pure git helper — None is 'that ref does not resolve'. Its one caller is cmd_self "
            "(`self --pin`), which dies on it; that refusal is loud and asserted, never silent",

    # --- EXCLUDED: the "nothing" is the LOUD direction. These resolvers refuse by returning None,
    # and the refusal is a die() in the caller that the suite asserts many times over. Neutering
    # them makes every claim refuse — noisy, not silent. The silent failure here is the INVERSE
    # (resolving a path it should not), and that mutation is outside this sweep's shape (INV6).
    ".game_loop/bin/game_loop::resolve_read": "its None is the REFUSAL, and the refusal is loud — `claim --read` dies on it "
                    "and that death is asserted repeatedly. The silent direction is the inverse "
                    "(resolving what it should not), which is the mutation this sweep cannot make",
    ".game_loop/bin/game_loop::resolve_env": "same as resolve_read, for a pin's anchor: None makes the command die, which is "
                   "asserted; the dangerous direction is accepting an anchor that does not exist",

    # --- EXCLUDED: loaders and normalizers. Their "nothing" is a state the caller branches on, not
    # a report that was withheld.
    ".game_loop/bin/game_loop::sanitize_session": "a normalizer, not a detector — None means 'not a usable session id' and "
                        "the caller branches to repo-global state. Neutering it changes WHICH "
                        "state file is used, which fails loudly across the session-scoping tests",
    ".game_loop/bin/game_loop::load_limits": "a loader — None is 'no limits file yet', the ordinary first-run state, and the "
                   "callers already treat it as empty ((load_limits() or {}))",
    ".game_loop/bin/game_loop::installed_version": "a loader — None is 'no VERSION file', which is documented as the "
                         "game_loop source repo's own state. The producer that turns this into a "
                         "verdict is update_notice, and that IS swept",
    ".game_loop/bin/game_loop::_scan_text": "None is a STATED skip ('too big to grep'), not a finding withheld; --expect "
                  "reports UNCHECKED rather than ✓ when it gets nothing, and that is asserted",

    # --- EXCLUDED: a formatter whose silence is configured, and helpers of producers now swept.
    ".game_loop/bin/game_loop::flair_lines": "a formatter, and its empty list is a configured opt-out (no flair module) "
                   "rather than a verdict about anything. Silence here is the shipped default",
    ".game_loop/bin/game_loop::_home_keyed": "helper of config_paths_report — its None is 'not under anyone's home', the "
                   "ordinary case for every entry. config_paths_report is a MUTANTS entry and "
                   "sweeps both of this helper's arms",
    ".game_loop/bin/game_loop::main_checkout": "helper of worktree_drift — its None is 'this IS the main checkout', the "
                     "ordinary case. worktree_report is a MUTANTS entry",
    ".game_loop/bin/game_loop::_same_bytes": "helper of worktree_drift — its None is UNREADABLE, which worktree_report turns "
                   "into an explicit UNKNOWN line rather than 'matching'; swept there",
    ".game_loop/bin/game_loop::admit_distribution": "its verdict is delivered by die(), not by this return value — the "
                          "return is the record it writes afterwards. Neutering the body deletes "
                          "those refusals and would re-measure the dominance gate, which already "
                          "has its own MUTANTS entry",

    # --- KNOWN GAPS. Real producers. Should be swept. Are not, and the reason is cost, not merit:
    # each MUTANTS entry is one full suite run (~1 min), and this change spent its budget on the
    # four report producers that were actually found weak. These are the queue, in this order.
    ".game_loop/bin/game_loop::retro_nudge": "KNOWN GAP still, but the REASON has changed and the "
                   "old one would now be false. It was measured at 0 kills and the note said the "
                   "remedy was an assertion nobody had written. That assertion now exists: the nudge "
                   "turned out to be ARITHMETICALLY UNREACHABLE — it counted a verb that had run once "
                   "in the entire log against a threshold of 12 — and the fix carries paired tests "
                   "that fire it from a second counter with zero transitions. So it is no longer "
                   "unasserted; it is owed a RE-measure, and no floor may be recorded until that runs",
    ".game_loop/bin/game_loop::legacy_mandate_warning": "KNOWN GAP, same as retro_nudge and found the same way. A real "
                              "warning producer of unpushed_warning's shape, MEASURED at 0 kills "
                              "against this HEAD — the legacy-mandate warning can stop firing and "
                              "this suite says nothing. Owed an assertion, then a MUTANTS entry",
    ".game_loop/bin/game_loop::pinned_report": "KNOWN GAP. A real report producer (the PINNED CODE block) whose empty list "
                     "is the common path, which is precisely the silence-on-pass shape. Left out "
                     "for run time; it should be swept",
    ".game_loop/bin/game_loop::metric_movement": "KNOWN GAP. A real detector — 'has this metric moved, and by how much' — "
                       "and its None is a non-event that several commands print around. Left out "
                       "for run time; it should be swept",
    # --- FOUND ONLY BY THE SECOND SIGNATURE. Both were invisible while the discriminator looked
    # for a literal empty return, which is why the accounting read "0 unaccounted" over a short
    # denominator. Neither is excluded on merit; both are queued.
    ".game_loop/bin/game_loop::binding_windows": "KNOWN GAP, and the sharpest one here. It decides which usage windows are "
                       "BINDING, and an always-empty return means no window ever binds — the "
                       "limitgate stops firing and a run sails into an exhausted limit with no "
                       "handoff written, silently. Exactly the shape this file distrusts, and it "
                       "was invisible to the first signature. Owed an assertion, then a MUTANTS "
                       "entry, ahead of the other gaps",
    ".game_loop/bin/game_loop::parse_events": "KNOWN GAP. Parses the per-event distribution behind the dominance refusal "
                    "(INV7). An always-empty return means no distribution is ever seen, so the "
                    "one-event-dominates check cannot fire. `dominance` IS swept and would catch "
                    "some of this, but not a parse that silently yields nothing",
    ".game_loop/bin/game_loop::_asked_the_user": "KNOWN GAP. A real detector whose False is a non-event ('this turn did not "
                       "ask the user'), which is the shape this file exists to distrust. Left out "
                       "for run time; it should be swept",

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
    ".game_loop/bin/watchdog::superseded":
        "KNOWN GAP, and now a MEASURED one: 0 kills against a baseline of 534. Nothing in the "
        "suite notices if it stops working, so every ring decision that branches on -- has a newer "
        "watchdog taken over? -- is unasserted. Not listed in MUTANTS only because a standing "
        "UNPROTECTED entry makes the sweep exit 1 with no path to green (INV5); the remedy is an "
        "assertion nobody has written yet. Same standing debt as retro_nudge, same shape.",
    ".game_loop/bin/notify.py::replies":
        "KNOWN GAP of a different kind: this producer cannot be measured by this tool AT ALL. "
        "Neutered to its own nothing-literal the suite HANGS rather than fails -- 300s cap hit, "
        "the whole baseline reported as killed because no assertion finished. watchdog's "
        "poll_slack_replies is a `while True:` whose three exits all wait on a reply arriving, so "
        "a producer that never produces spins forever. That is right in production and wrong for "
        "a test, which has no bound of its own. A hang is worse than a failure: it yields no "
        "verdict, and from outside it looks like a slow machine. Tracked as issue #50 -- and it "
        "is why an earlier sweep of these ten stalled with no output at all.",
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
    return isinstance(v, ast.List) and not v.elts    # `return []`


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
    return any(isinstance(n, ast.Return) and isinstance(n.value, ast.Name)
               and n.value.id in empties for n in ast.walk(fn))


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
    if r.returncode != 0:
        return [BIN]                      # no git: fall back to the one file, and say nothing false
    out = []
    for rel in (ln.strip() for ln in r.stdout.split("\n")):
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
    baseline = set(passing(run(base)))
    print(f"baseline: {len(baseline)} named assertions pass unmutated\n", flush=True)

    verdicts = []
    for label, key, body, marks, thin_note, floor in MUTANTS:
        rel, fn = key.split("::", 1)
        t = tempfile.mkdtemp(prefix="sweep-")
        try:
            try:
                with open(os.path.join(base, rel)) as f:
                    original = f.read()
            except OSError:
                print(f"  !! {key}: {rel} is not in the tree under test. Nothing was swept.\n")
                verdicts.append((key, None, UNPROTECTED, floor))
                continue
            mutated, hit = neuter(original, fn, body)
            if not hit:
                # Not a skip. A producer named here that no longer exists is zero evidence about
                # zero code, and a sweep that shrugs at that is a check that cannot fail.
                print(f"  !! {key}: NOT FOUND in {rel} — renamed, or gone. Nothing was swept.\n")
                verdicts.append((key, None, UNPROTECTED, floor))  # 4-wide: the tallies unpack it
                continue
            shutil.copytree(base, t, dirs_exist_ok=True)
            with open(os.path.join(t, rel), "w") as f:
                f.write(mutated)
            os.chmod(os.path.join(t, rel), 0o755)
            out = run(t)
            still = set(passing(out))
            killed = len(baseline - still)
            v = verdict(killed)
            verdicts.append((key, killed, v, floor))
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
    # KNOWN GAPS are declared, not silent. NOT_SWEPT holds two kinds of entry — a genuine exclusion
    # and a DEBT — and printing only the first kind turns the debt into a denylist with extra steps:
    # decided once, then never read again. The run says how much is owed, every time.
    gaps = sorted(k for k, why in NOT_SWEPT.items() if (why or "").strip().startswith("KNOWN GAP"))
    if gaps:
        print(f"KNOWN GAPS — declared, NOT swept ({len(gaps)}): " + " · ".join(gaps))
        print("Each should be swept and is not yet, with its reason recorded beside it.")
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
