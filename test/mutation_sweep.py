"""Producer mutation sweep: neuter one silence-on-pass producer at a time and see which
assertions survive.

A guard's "allow" is silence; so is a detector that finds nothing, a validator with no
complaints, and a nudge that declines to fire. Every "stays quiet" / "gets no nudge" /
"passes untouched" assertion is satisfied by a producer that has stopped working.

Each entry rewrites one function's body to the neutered form, runs the suite, and reports
the checks that STILL PASS whose text suggests they are about that producer's silence.
"""
import os, re, shutil, subprocess, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = ".game_loop/bin/game_loop"

# (label, function name, neutered body, substrings that mark an assertion as being about it)
MUTANTS = [
    ("unpushed_warning -> never warns", "unpushed_warning", "    return None\n",
     ["unpushed", "upstream", "quiet"]),
    ("fix_warning -> never warns", "fix_warning", "    return None\n",
     ["fix", "quiet", "silence"]),
    ("category_tell -> never detects", "category_tell", "    return None\n",
     ["nudge", "category", "scope"]),
    ("aggregate_tell -> never detects", "aggregate_tell", "    return None\n",
     ["nudge", "aggregate", "sum"]),
    ("dominance -> never finds an outlier", "dominance", "    return None\n",
     ["dominan", "distribution", "spread", "event"]),
    ("ruled_out -> finds no refutations", "ruled_out", "    return []\n",
     ["ruled", "refut"]),
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


base = tempfile.mkdtemp(prefix="sweep-base-")
subprocess.run(f"git -C {REPO} archive HEAD | tar -x -C {base}", shell=True, check=True)
with open(os.path.join(base, BIN)) as f:
    original = f.read()

print("producer mutation sweep — assertions that SURVIVE a neutered producer\n")
for label, fn, body, marks in MUTANTS:
    t = tempfile.mkdtemp(prefix="sweep-")
    try:
        shutil.copytree(base, t, dirs_exist_ok=True)
        mutated, found = neuter(original, fn, body)
        if not found:
            print(f"  !! {fn}: not found, skipped\n")
            continue
        with open(os.path.join(t, BIN), "w") as f:
            f.write(mutated)
        os.chmod(os.path.join(t, BIN), 0o755)
        out = run(t)
        tail = out.strip().split("\n")[-1]
        survivors = [c for c in passing(out)
                     if any(m in c.lower() for m in marks)]
        print(f"{label}\n  suite: {tail}")
        for c in survivors:
            print(f"    SURVIVED: {c[:96]}")
        if not survivors:
            print("    (nothing in this producer's domain survived)")
        print()
    finally:
        shutil.rmtree(t, ignore_errors=True)
shutil.rmtree(base, ignore_errors=True)
