#!/usr/bin/env python3
"""Decide WHAT the nightly sweep actually found, and say only that.

THE REPORT USED TO READ THE JOB'S EXIT AND CLAIM A FACT ABOUT COVERAGE (#128). It fired on
`needs.sweep.result != 'success'` and then filed an issue titled "a producer is unprotected or has
drifted" — a claim about the SWEEP, derived from the JOB. Measured on run 34227453406: eleven of
twelve shards succeeded, the twelfth ran a completely clean sweep ("no producer is unprotected, and
none is below its recorded floor") and then died uploading its artifact with a 403. The issue named
a coverage regression that had not happened, and sat unread for eight days.

That is INV2's rule in CI: a check that decides about X by measuring Y is reading a proxy, and a
proxy that merely correlates eventually disagrees with the thing it stands for.

THREE OUTCOMES, WHICH IS INV8's SHAPE ONE LAYER OUT. A run that is not green has three causes and
they want different words:

  * REGRESSED   — a shard reported a real verdict. The only one that is about coverage.
  * INCOMPLETE  — a shard's log is missing, so the set we judged is SHORTER than the set that ran.
                  "The logs I have are clean" is not "the sweep was clean", and collapsing those
                  two is how a short denominator reports success.
  * INFRA       — every expected log is present and clean, and the job still failed. Nothing is
                  wrong with the code; say so rather than sending someone hunting coverage.

Exits 0 having written the body when there is something to file, 1 when there is not.
"""
import glob
import os
import sys

VERDICT_PREFIXES = ("UNPROTECTED", "INERT", "NOT MEASURED", "DRIFT")
CLEAN = "no producer is unprotected, and none is below its recorded floor."


def classify(logs_dir, expected, job_ok):
    found = sorted(glob.glob(os.path.join(logs_dir, "*", "sweep-*.log")))
    texts = {}
    for p in found:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                texts[os.path.basename(p)] = f.read()
        except OSError:
            pass

    regressed = sorted({
        ln.strip()
        for t in texts.values() for ln in t.splitlines()
        if ln.startswith(VERDICT_PREFIXES)
    })
    clean = [n for n, t in texts.items() if CLEAN in t]
    missing = expected - len(texts)

    if regressed:
        return "REGRESSED", regressed, texts, missing
    if missing > 0:
        return "INCOMPLETE", [], texts, missing
    if job_ok:
        return "GREEN", [], texts, 0
    return "INFRA", [], texts, 0


def render(kind, regressed, texts, missing, expected, run_url):
    L = []
    if kind == "REGRESSED":
        title = "mutation sweep: a producer is unprotected or has drifted"
        L.append("The nightly sweep reported a verdict a reader has to act on.")
    elif kind == "INCOMPLETE":
        title = "mutation sweep: %d shard log(s) missing — the run was NOT measured" % missing
        L.append("**The sweep did not report completely, so nothing here says coverage is fine.**")
        L.append("")
        L.append("%d of %d shard logs are present. The ones that are present may all be clean, and "
                 "that is not the same claim: a verdict over a SHORTER set than the one that ran is "
                 "how a short denominator reports success." % (len(texts), expected))
    else:  # INFRA
        title = "mutation sweep: the run failed but every shard came back CLEAN (infrastructure)"
        L.append("**No coverage problem. Every one of the %d expected shard logs is present and "
                 "reports the clean verdict, and the job still failed.**" % expected)
        L.append("")
        L.append("Whatever failed is in the run itself — an upload, a runner, a network call — not "
                 "in the producers. Filed under its own title so nobody is sent hunting a "
                 "regression that did not happen; that mistake cost eight unread days once (#128).")
    L += ["", "Run: %s" % run_url, ""]
    if kind == "REGRESSED":
        L += ["A sweep fails when a producer is UNPROTECTED (neutering it killed no assertion),",
              "INERT (nothing calls it where an assertion can see), NOT MEASURED (the mutant never",
              "applied, or its suite crashed or hung), or has DRIFTED below a recorded floor. Only",
              "the last is a regression in coverage; the others are gaps.", "",
              "The verdicts, verbatim:", "", "```"] + regressed + ["```"]
    L += ["", "Shard logs seen: %d of %d." % (len(texts), expected), "",
          "Opened by `.github/workflows/mutation-sweep.yml`. While it stays open no further issues",
          "are filed, so close it once the sweep is clean again."]
    return title, "\n".join(L)


def main():
    logs_dir = sys.argv[1]
    expected = int(sys.argv[2])
    job_ok = sys.argv[3] == "success"
    run_url = sys.argv[4] if len(sys.argv) > 4 else "(no run url)"
    kind, regressed, texts, missing = classify(logs_dir, expected, job_ok)
    if kind == "GREEN":
        sys.stderr.write("sweep is clean and the job succeeded — nothing to file\n")
        return 1
    title, body = render(kind, regressed, texts, missing, expected, run_url)
    with open("body.md", "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print(title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
