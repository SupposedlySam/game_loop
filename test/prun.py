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
        listed.append((ln, m.group(2).rstrip(), len(raw) - len(raw.lstrip())))
    groups, cur = [], None
    for ln, name, indent in listed:
        if indent <= 4:
            cur = (name, [name])
            groups.append(cur)
        elif cur is not None:
            cur[1].append(name)
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
    distinct, total = len(merged), passed + failed
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
    return 1 if (failed or silent) else 0


if __name__ == "__main__":
    sys.exit(main())
