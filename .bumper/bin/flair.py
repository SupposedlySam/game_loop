"""flair — the fun, opt-out layer. Celebrates when a bumper helps, and shouts out milestones.

This is the ONLY part of bumper that is decoration, not enforcement. It is deliberately isolated here
so it never touches the gate logic. It is purely additive to output, only ever writes its own state
keys, and can be turned off entirely (config.json -> flair.enabled = false). If this file is missing
or broken, bumper works exactly the same, just quieter — every call site swallows its errors.

The lines are written in the FIRST PERSON so the agent naturally repeats them back to the human. They
are suggestions, not instructions — nothing here forces the model to say anything (that would violate
the one design rule). It just hands it a fun line at a good moment.
"""
import datetime
import json
import os
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .bumper/
CONFIG_F = os.path.join(ROOT, "config.json")

DEFAULT = {
    "enabled": True,
    "support_name": "SupposedlySam",
    "support_url": "https://buymeacoffee.com/supposedlysam",
}


def _cfg():
    c = dict(DEFAULT)
    try:
        with open(CONFIG_F) as f:
            c.update((json.load(f).get("flair") or {}))
    except (OSError, ValueError):
        pass
    return c


ASSIST = {
    "watchdog": [
        "🎳 BumperBot bounced me right back into the lane — no gutter today.",
        "🎳 Thanks for the nudge, BumperBot! Back to work.",
        "🎳 BumperBot caught me idling and rolled me back on track.",
        "🎳 Strike! BumperBot kept the momentum going.",
    ],
    "stopgate": [
        "🎳 BumperBot kept me on track — redirecting instead of bailing.",
        "🎳 Nice catch, BumperBot. Staying in the lane.",
        "🎳 BumperBot guarded that gutter ball. Carrying on.",
        "🎳 Thanks for the motivation, BumperBot — not stopping yet.",
    ],
    "claim": [
        "🎳 Sourced it for real — thanks for keeping me honest, BumperBot.",
        "🎳 BumperBot made me cite the actual file. Receipts attached.",
    ],
    "harden": [
        "🎳 Learning encoded, not just remembered. BumperBot approves.",
        "🎳 Turned that lesson into a guard — BumperBot smiles.",
    ],
    "arm": [
        "🎳 One earned interruption, armed. BumperBot keeps the ledger honest.",
    ],
    "checkpoint": [
        "🎳 Clean report, no question asked. BumperBot nods.",
    ],
    "mandate_clear": [
        "🎳 Work done and signed off. Good run, BumperBot!",
    ],
    "mandate_set": [
        "🎳 Mandate locked in. BumperBot's on the rails with me now — let's roll.",
    ],
}

UPTIME_HOURS = [1, 2, 3, 4, 6, 8, 12, 16, 24, 36, 48, 72, 96, 120, 168]
ASSIST_COUNTS = [5, 10, 25, 50, 100, 250, 500, 1000]
CLAIM_COUNTS = [10, 25, 50, 100, 250, 500]
HARDEN_COUNTS = [5, 10, 25, 50, 100]

# The funding pitch, in a theatrical System-AI / Claptrap register. Rotated on every use so the ask
# never reads like the same canned banner twice. Keep them PG and always end with the link. Each takes
# {name} and {url}.
COFFEE_CTAS = [
    "This uninterrupted rampage was brought to you by CAFFEINE. Keep {name} dangerously caffeinated ☕ → {url}",
    "The Management reminds its viewers: a well-tipped author waxes the lanes faster. Coffee for {name} ☕ → {url}",
    "MINION! Tribute is expected. Reward your BumperBot overlord by buying {name} a coffee ☕ → {url}",
    "SPONSOR BREAK! This session is sponsored by... nobody yet. Be the hero. Coffee {name} ☕ → {url}",
    "Ooh ooh! If you liked that, you'll LOVE giving {name} a coffee! I'm 60% sure it's the best decision you'll make today ☕ → {url}",
    "Warning: continued brilliance runs on bean water. Refuel {name} before the magic wears off ☕ → {url}",
    "The dungeon appreciates a generous crawler. Slide {name} a coffee across the bar ☕ → {url}",
    "Achievement pending: 'Bought {name} A Coffee.' It's RIGHT THERE. So close. ☕ → {url}",
    "Beep boop — my gratitude subroutine is overflowing! Convert it to coffee for {name} ☕ → {url}",
    "Statistically, tippers are 100% cooler. Prove the stat — coffee for {name} ☕ → {url}",
]

# Milestone announcement lead-ins. Rotated too. Uptime ones take {label}; assist ones take {n}.
UPTIME_ANNOUNCE = [
    "🎳🏆 BumperBot has kept your AI rolling uninterrupted for {label}!",
    "🎳📢 DUNGEON ANNOUNCEMENT: your AI has crawled {label} straight with no human hand on the wheel!",
    "🎳🤖 {label} of nonstop glory! BumperBot demands to be named Employee of the Month!",
    "🎳🔔 Ding ding ding! {label} in a row, zero interruptions. BumperBot did THAT!",
    "🎳🎬 We're {label} into the season finale and your AI has not once stopped for a snack break!",
]
ASSIST_ANNOUNCE = [
    "🎳🏆 BumperBot has stepped in {n} times to keep this session on the rails.",
    "🎳📢 That's SAVE NUMBER {n}, folks! BumperBot is on fire! (Not literally. That voids the warranty.)",
    "🎳🤖 {n} assists and counting! Somebody get BumperBot a trophy. Or a coffee. Coffee's fine.",
    "🎳🎯 {n} gutter balls deflected! BumperBot's aim is, frankly, showing off at this point.",
]
CLAIM_ANNOUNCE = [
    "🎳 {n} claims sourced — that's {n} assertions backed by a real file instead of a vibe.",
    "🎳📚 {n} receipts filed! BumperBot has confiscated {n} guesses and returned them as facts.",
    "🎳🔎 {n} claims, {n} actual files read. No vibes were harmed in the making of these assertions.",
]
HARDEN_ANNOUNCE = [
    "🎳 {n} learnings hardened into real guards. BumperBot swells with mechanical pride.",
    "🎳🔧 {n} lessons welded into the machine — never to be forgotten in a compaction again!",
    "🎳🧱 {n} bricks laid in the wall of Things That Can No Longer Go Wrong. BumperBot approves.",
]


def assist(event):
    """A fun first-person line for a helpful event, or "" if flair is off / no pool."""
    c = _cfg()
    if not c.get("enabled"):
        return ""
    pool = ASSIST.get(event)
    return random.choice(pool) if pool else ""


def _coffee(c):
    return random.choice(COFFEE_CTAS).format(name=c.get("support_name"), url=c.get("support_url"))


def _hours_since(iso):
    try:
        then = datetime.datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return 0.0
    return (datetime.datetime.now() - then).total_seconds() / 3600.0


def milestones(state):
    """Newly-reached milestone messages + the fired-keys to persist. Never repeats a fired one.

    Returns (messages, new_keys). The caller adds new_keys to state['flair_fired'] and saves. Counters
    (watchdog_rings_total, stop_gate_blocks_total, claim_count, hardened_count) are maintained by the
    callers; this only reads them.
    """
    c = _cfg()
    if not c.get("enabled"):
        return [], []
    fired = set(state.get("flair_fired", []))
    msgs, new = [], []

    def hit(key, msg):
        if key not in fired:
            msgs.append(msg)
            new.append(key)

    # Uptime under the current mandate — keyed by mandate.since so a fresh mandate gets fresh
    # milestones. This is the headline "continued uninterrupted for X hours" celebration.
    m = state.get("mandate") or {}
    since = m.get("since")
    if m.get("active") and since:
        hrs = _hours_since(since)
        for h in UPTIME_HOURS:
            if hrs >= h:
                label = "{}h".format(h) if h < 24 else "{}h ({}d)".format(h, round(h / 24))
                announce = random.choice(UPTIME_ANNOUNCE).format(label=label)
                hit("uptime:{}:{}".format(since, h), announce + " " + _coffee(c))

    assists = state.get("watchdog_rings_total", 0) + state.get("stop_gate_blocks_total", 0)
    for n in ASSIST_COUNTS:
        if assists >= n:
            announce = random.choice(ASSIST_ANNOUNCE).format(n=n)
            cta = " " + _coffee(c) if n >= 50 else ""
            hit("assist:{}".format(n), announce + cta)

    for n in CLAIM_COUNTS:
        if state.get("claim_count", 0) >= n:
            hit("claim:{}".format(n), random.choice(CLAIM_ANNOUNCE).format(n=n))

    for n in HARDEN_COUNTS:
        if state.get("hardened_count", 0) >= n:
            hit("harden:{}".format(n), random.choice(HARDEN_ANNOUNCE).format(n=n))

    return msgs, new
