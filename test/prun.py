#!/usr/bin/env python3
"""Run test/run.py's sections across processes, and merge the results honestly.

WHY THIS EXISTS. The suite is 1719 assertions in ~306 seconds on 14 cores, and it uses one of them.
The cost is not compute: nearly every section builds a sandbox and then spawns the 14k-line binary
several times, so the run is dominated by process startup that parallelises perfectly.

WHY IT SHARDS BY SECTION rather than by assertion. `--section` already exists, is already how
verify.yaml gates a change on what it touches, and #105 established the soundness rule for it:
top-level sections are independently selectable, and any subset of a NESTED sequence is extended to
a leading slice. Reusing that selector means this runner inherits a property somebody already
measured instead of inventing one.

WHAT IT DOES NOT CLAIM. Sections are selected independently; they are not PROVEN independent when
run CONCURRENTLY. Two sections that write the same absolute path would race here and not in a serial
run. `--verify` exists for exactly that: it runs the shards, runs the whole suite serially, and
diffs every assertion NAME and VERDICT between them. A speedup that changes an outcome is not a
speedup, and the only honest way to know is to compare.
"""
import argparse, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "run.py")
TIMINGS = os.path.join(HERE, ".section-times.json")
FLOOR = os.path.join(HERE, "suite-floor.json")
COUNT = re.compile(r"^(\d+) passed, (\d+) failed", re.M)
OUTCOME = re.compile(r"^  (ok|FAIL)\s+(.*)$", re.M)


def sections():
    """[(top_level_name, [names to select for it])] — a parent and the nested sections it owns.

    SHARDING TOP-LEVEL NAMES ALONE SILENTLY LOSES CHECKS, and `--verify` caught it: 10 assertions
    present in a serial run were ABSENT from the parallel one — #88's setter record and four of
    #94's — while the parallel run cheerfully reported MORE outcomes than the suite has, because
    duplication elsewhere masked the loss. A shard that covers less and counts more is the short
    denominator again, arriving in the thing built to make the suite faster.

    So a nested section is selected EXPLICITLY, in the same shard as the top-level section it sits
    under — nearest preceding top-level by line number. Selecting the parent does not reliably reach
    it, and the runner must not assume the selector's closure will.
    """
    r = subprocess.run([sys.executable, RUN, "--list-sections"], capture_output=True, text=True)
    src = open(RUN).read().split("\n")
    listed = []
    for line in r.stdout.splitlines():
        m = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if not m:
            continue
        ln = int(m.group(1))
        raw = src[ln - 1] if 0 < ln <= len(src) else ""
        name = m.group(2).rstrip()
        # THE TRAILER IS NOT A SECTION. `--list-sections` ends with "138 sections. Select with: …",
        # which starts with a number and therefore matched this regex — so a phantom section has
        # been in this list all along, passed to a shard as a `--section` that names nothing, and
        # counted in the group total. Whether it landed as a TOP-LEVEL group depended on the
        # indentation of whichever source line happened to share its number, so the count moved for
        # reasons having nothing to do with the suite.
        #
        # Found by the new floor on its first real use, which is the argument for the floor: the
        # phantom cost nothing visible for as long as nobody compared the number to anything.
        #
        # The discriminator is that a REAL section's name appears in its own source line — it is
        # printed there. Measured across the whole list: 138 of 138 real sections satisfy that, and
        # the trailer is the only line that does not.
        if not (name and name in raw):
            continue
        listed.append((ln, name, len(raw) - len(raw.lstrip())))
    # NESTED SECTIONS WITH NO PRECEDING TOP-LEVEL ONE WERE DROPPED ON THE FLOOR. `cur is None`
    # until the first indent<=4 section, and in this suite the shared-fixture block opens FIRST:
    # 57 of 138 sections precede any top-level one, so they were in no group, named by no shard,
    # and absent from every count this file prints. The docstring above promises a nested section is
    # selected explicitly "in the same shard as the top-level section it sits under" — for these
    # there is no such section, and the loop silently skipped them rather than saying so.
    #
    # They still RAN, which is why nothing noticed: run.py extends any subset of a nested sequence
    # to a leading slice, so selecting anything in that block pulls them in. That is a property of
    # the selector, not of this runner, and it is exactly the "the runner must not assume the
    # selector's closure will reach it" the docstring warns about — with this file doing the
    # assuming. `--verify` is the check that they still run; it is not a reason to leave them
    # unnamed.
    #
    # They become ONE group: they share a fixture and the leading-slice rule means selecting the
    # last pulls the rest anyway, so splitting them would buy no parallelism and pay the prefix
    # cost repeatedly.
    groups, cur = [], None
    leading = []
    for ln, name, indent in listed:
        if indent <= 4:
            cur = (name, [name])
            groups.append(cur)
        elif cur is not None:
            cur[1].append(name)
        else:
            leading.append(name)
    if leading:
        groups.insert(0, (leading[0], list(leading)))
    return groups


def shard(groups, jobs):
    """Longest-processing-time-first, using measured section times when we have them.

    LPT because the run is long-tailed: one section is 37.8s and the median is under a second, so
    round-robin leaves a shard holding the tail while thirteen idle. Unknown sections are assumed
    average rather than zero — assuming zero packs every new section into one bin.
    """
    try:
        with open(TIMINGS) as f:
            known = json.load(f)
    except (OSError, ValueError):
        known = {}
    avg = (sum(known.values()) / len(known)) if known else 1.0
    order = sorted(groups, key=lambda g: known.get(g[0], avg), reverse=True)
    bins = [[] for _ in range(jobs)]
    load = [0.0] * jobs
    for top, names in order:
        i = load.index(min(load))
        bins[i].extend(names)               # parent AND its nested sections, together
        load[i] += known.get(top, avg)
    return [b for b in bins if b]


def run_shard(names):
    cmd = [sys.executable, RUN]
    for n in names:
        cmd += ["--section", n]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    return {"names": names, "sec": time.time() - t0, "rc": r.returncode,
            "out": r.stdout + r.stderr}


def floor_breaches(floor, distinct, n_groups):
    """Which recorded floors this run came in UNDER, as printable lines. Empty when it did not.

    At module level for the reason every selector here is: a rule that lives inside main() cannot be
    driven by the suite, and a floor nobody can drive is one that gets believed rather than checked.

    A MISSING OR NON-INT ENTRY IS NOT A BREACH. An absent floor means nothing was recorded, which is
    a different thing from a floor of zero — and reading absence as satisfied is how the runner
    would report a clean bill for a file it could not parse. main() says so separately when the
    whole file is missing, rather than letting silence here stand in for it.
    """
    out_ = []
    for key, now, what in (("distinct", distinct, "distinct assertion(s)"),
                           ("groups", n_groups, "section group(s)")):
        want = floor.get(key)
        if isinstance(want, int) and not isinstance(want, bool) and now < want:
            out_.append(f"{what}: {now}, floor {want} (down {want - now})")
    return out_


def outcomes(text):
    return {m.group(2).strip(): m.group(1) for m in OUTCOME.finditer(text)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", "--jobs", type=int, default=0, help="0 = one per core")
    ap.add_argument("--verify", action="store_true",
                    help="also run the whole suite serially and diff every outcome")
    ap.add_argument("--time", action="store_true",
                    help="record per-section times for future balancing")
    a = ap.parse_args()
    jobs = a.jobs or (os.cpu_count() or 4)
    groups = sections()
    secs = [n for _t, ns in groups for n in ns]
    if not secs:
        print("prun: the suite listed NO sections — refusing to report a pass over nothing.")
        return 2
    # BEFORE shard() rebinds the name: `groups` goes from "the suite's section groups" to "the
    # per-worker bins", and the floor is about the former. Comparing the wrong one made the first
    # run of this floor report a breach of 81 against 14 bins — caught by running it.
    n_groups = len(groups)
    groups = shard(groups, jobs)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=len(groups)) as ex:
        results = list(ex.map(run_shard, groups))
    wall = time.time() - t0

    passed = failed = 0
    merged = {}
    silent = []                    # shards that never reported — see below
    for r in results:
        m = COUNT.search(r["out"])
        if m:
            passed += int(m.group(1)); failed += int(m.group(2))
        else:
            # A SHARD THAT NEVER REPORTED IS NOT A SHARD THAT PASSED. Without this the totals were
            # summed only over shards that produced a trailer, so a shard which CRASHED contributed
            # nothing to either number — and `1 if failed else 0` then returned 0. Measured: one
            # deliberate RuntimeError inside a section gave `1912 passed, 0 failed`, exit 0, and no
            # mention of the traceback anywhere in this output. 180 assertions never ran and the
            # gate was green, which is what `verify` runs for the whole-suite rules.
            #
            # This is the third outcome the summary did not have: passed, failed, and NEVER
            # ANSWERED. Counting the shard as a failure would be a different lie — nothing in it
            # failed — so it gets its own name and its own exit.
            silent.append(r)
        merged.update(outcomes(r["out"]))
        if r["rc"] != 0:
            for line in r["out"].splitlines():
                if line.startswith("  FAIL"):
                    print(line)
    print("%d passed, %d failed  ·  %d shard(s) on %d job(s), %.1fs wall" %
          (passed, failed, len(groups), jobs, wall))
    # A SUM OVER SHARDS IS NOT AN ASSERTION COUNT, and this line was read as one. A shared fixture
    # prefix re-runs in every shard that needs it, so the totals above exceeded the suite's own
    # serial count by 276 — and that gap was mistaken for LOST COVERAGE by the first person to
    # compare the two numbers, which was me. `--verify` then showed the distinct sets identical:
    # 1871 either way, nothing absent in either direction, no verdict differing. Nothing was lost;
    # the totals were counting the same assertion in several shards.
    #
    # This is the short-denominator shape the docstring warns about, arriving in the SUMMARY rather
    # than in the sharding: a shard that covers less can still count more. So the line now says
    # what it is a sum OF, because a number that cannot be compared to anything is not evidence —
    # and the one comparison a reader will reach for is the serial run this is meant to replace.
    # A SHORT RUN IS NOT A SMALL SUITE (showrunner, #104). The zero case is guarded above —
    # `sections()` returning nothing is refused rather than reported as a pass over nothing. The
    # case that actually happens is SHORT: a parse change in --list-sections, a header style that
    # stops matching, a shard-builder that drops a group. Then every shard that DID run passes, the
    # total is merely smaller, and this exits 0. showrunner emptied their dispatch tuple and got
    # "16 passed, 0 failed, exit 0" — not zero, a plausible small green run, which their release
    # gate accepted. Their point is the one worth stealing: a suite cannot notice its own absence
    # using an expectation that shrinks with it.
    #
    # So the expectation lives OUTSIDE the run, in a recorded floor, and is raised deliberately.
    # That is the same tripwire the mutation sweep already uses per producer, and it costs an
    # explicit update whenever assertions are legitimately removed — which is the point: a
    # DECISION, rather than a number nobody was watching.
    distinct, total = len(merged), passed + failed
    floor = {}
    try:
        with open(FLOOR) as f:
            floor = json.load(f)
    except (OSError, ValueError):
        pass
    below = floor_breaches(floor, distinct, n_groups)
    if below:
        print("\nSUITE FLOOR BREACHED — this run covered LESS than the recorded floor, and every\n"
              "  shard that ran passed, so nothing else here would have said so:")
        for _b in below:
            print("    " + _b)
        print("  A short run is not a small suite. Either a section stopped being FOUND (check\n"
              "  --list-sections), or assertions were removed on purpose — in which case lower the\n"
              "  floor in test/suite-floor.json in the same commit, so it is a decision on the\n"
              "  record rather than a number that quietly followed the code down.")
    elif floor.get("distinct") and distinct > floor["distinct"]:
        print("  floor: %d distinct (this run %d, +%d) — raise it in test/suite-floor.json when "
              "this settles" % (floor["distinct"], distinct, distinct - floor["distinct"]))
    elif not floor:
        print("  floor: NOT RECORDED — test/suite-floor.json is missing or unreadable, so a run "
              "that covered less than the last one would not be noticed here.")
    if total != distinct:
        print("  %d distinct assertion(s) · %d outcome(s) counted more than once (a shared fixture"
              "\n  prefix re-runs in every shard that needs it). The totals above are a SUM OVER"
              "\n  SHARDS, NOT comparable to the serial runner's count — --verify diffs the NAMES."
              % (distinct, total - distinct))
    print("  slowest shard: %.1fs (%d sections)" %
          max((r["sec"], len(r["names"])) for r in results))
    for r in silent:
        print("\nSHARD NEVER REPORTED — it produced no `N passed, M failed` line, so none of its\n"
              "  assertions are in the totals above and their outcome is UNKNOWN, not passing.\n"
              "  sections: %s" % ", ".join(n[:60] for n in r["names"][:4]))
        tail = [l for l in r["out"].splitlines() if l.strip()][-3:]
        for l in tail:
            print("    " + l[:104])

    if a.time:
        # One section per shard is the only way to attribute time to a section honestly.
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            solo = list(ex.map(run_shard, [[n] for n in secs]))
        with open(TIMINGS, "w") as f:
            json.dump({r["names"][0]: round(r["sec"], 2) for r in solo}, f, indent=1)
        print("  recorded %d section times for balancing" % len(solo))

    if a.verify:
        print("\nverifying against a serial full run — a speedup that changes an outcome is not one")
        r = subprocess.run([sys.executable, RUN], capture_output=True, text=True)
        full = outcomes(r.stdout + r.stderr)
        missing = sorted(k for k in full if k not in merged)
        extra = sorted(k for k in merged if k not in full)
        differ = sorted(k for k in merged if k in full and merged[k] != full[k])
        print("  serial: %d outcomes · parallel: %d" % (len(full), len(merged)))
        print("  in serial but ABSENT from parallel : %d" % len(missing))
        print("  in parallel but absent from serial : %d" % len(extra))
        print("  present in both, DIFFERENT verdict : %d" % len(differ))
        for k in (missing[:5] + differ[:5]):
            print("      %s" % k[:100])
        if missing or differ:
            return 1
    # A SILENT SHARD FAILS THE RUN. It is not `failed` (nothing in it failed) and it must not be
    # 0 (nothing in it was established either) — the exit code is what `verify` reads, and a run
    # that lost a shard has not gated the paths that shard covered.
    # `below` joins the exit condition: a floor that reports and returns 0 is a floor `verify`
    # never reads, and verify's whole input from this runner is the exit code.
    return 1 if (failed or silent or below) else 0


if __name__ == "__main__":
    sys.exit(main())
