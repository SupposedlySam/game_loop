#!/usr/bin/env python3
"""Read a mutation-sweep log and report its VERDICTS. Run:  python3 test/sweep-verdicts.py <log>

WHY THIS EXISTS RATHER THAN A GREP. Assertion names in this repo quote the tool's own vocabulary, so
a grep for a verdict word also matches every assertion written ABOUT that verdict. `grep -c "BELOW
FLOOR"` on a healthy log returns hits from assertions named "...and a NOT MEASURED producer is never
also reported as BELOW FLOOR". The words cannot separate a verdict from prose about verdicts; the
COLUMN SHAPE can, and prose cannot imitate a column.

THE RULE WAS WRITTEN DOWN AND FAILED ANYWAY, four times. It sits in the handoff under "ANCHOR ON THE
FORMAT, NEVER ON THE WORDS", with the correct patterns spelled out — and I still hand-grepped the
words twice in one afternoon, once matching nothing (and killing a sweep whose answer was in the file
I called empty) and once matching too much (two phantom below-floors and three phantom unmeasured).
That is rung 6 failing in the way INV1 predicts, so this is the same knowledge at rung 4: the correct
reading is now the easy one.

READS A PARTIAL LOG ON PURPOSE. The sweep prints an honest trailer, but only at the end; the whole
reason to grep by hand is wanting to know at minute 20. So this takes whatever exists.
"""
import re
import sys

BELOW = re.compile(r"^\s+killed:\s+\d+\s+\[.*?\]\s+floor\s+\d+\s+.*BELOW FLOOR")
UNMEASURED = re.compile(r"^\s+!!\s")
REPORTED = re.compile(r"^\s+killed:\s+\d+")
PRODUCER = re.compile(r"^(\S[^\n]*?)\s+->\s")


def read(text):
    """(reported, below_floor, unmeasured) — each a list of producer names, in log order."""
    reported, below, unmeasured, current = [], [], [], None
    for line in text.splitlines():
        m = PRODUCER.match(line)
        if m:
            current = m.group(1)
            continue
        if REPORTED.match(line):
            reported.append(current)
            if BELOW.match(line):
                below.append(current)
        elif UNMEASURED.match(line):
            unmeasured.append(current)
    return reported, below, unmeasured


def main(argv):
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[0])
        return 2
    with open(argv[0]) as f:
        text = f.read()
    reported, below, unmeasured = read(text)
    print("  reported     : %d producer(s)" % len(reported))
    print("  BELOW FLOOR  : %d%s" % (len(below), "" if not below else " — " + ", ".join(below[:6])))
    print("  NOT MEASURED : %d%s" % (len(unmeasured),
                                     "" if not unmeasured else " — " + ", ".join(unmeasured[:6])))
    # THE CONTROL, printed every time: how many times the same words appear in assertion NAMES. If
    # this is non-zero while the counts above are zero, the naive grep would have lied to you, and
    # seeing that number is the point of the tool.
    noise = len(re.findall(r"SURVIVED:.*?(?:BELOW FLOOR|NOT MEASURED)", text))
    print("  (the same words appear in %d assertion NAME(s) — what a word-grep would have counted)"
          % noise)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
