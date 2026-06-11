"""
Base Rate Calculator
====================
Answers: if you randomly picked any player + window combination,
what percentage would show a 30+ point wOBA drop in the following
2 windows just by chance?

This is the null hypothesis comparison for the 57.1% combined
signal rate from validation_2025.py.

HOW TO RUN:
    python3 base_rate.py

REQUIRES:
    - statcast_cache/ folder from your validation run (already built)
    - swing_timing_rolling_2025.csv (to get the player ID list)

OUTPUTS:
    base_rate_summary.txt
"""

import csv
import json
import os
import random
from datetime import datetime
from collections import Counter

TIMING_FILE  = "swing_timing_rolling_2025.csv"
CACHE_DIR    = "statcast_cache"
OUTPUT_FILE  = "base_rate_summary.txt"

WOBA_DROP_THRESHOLD = 0.030   # same as validation script
MIN_PRE_WOBA        = 0.300   # same healthy baseline filter
RANDOM_SAMPLES      = 500     # number of random player+window picks
RANDOM_SEED         = 42

WINDOWS = [
    (1,'2025-04-01','2025-04-14'),(2,'2025-04-08','2025-04-21'),
    (3,'2025-04-15','2025-04-28'),(4,'2025-04-22','2025-05-05'),
    (5,'2025-04-29','2025-05-12'),(6,'2025-05-06','2025-05-19'),
    (7,'2025-05-13','2025-05-26'),(8,'2025-05-20','2025-06-02'),
    (9,'2025-05-27','2025-06-09'),(10,'2025-06-03','2025-06-16'),
    (11,'2025-06-10','2025-06-23'),(12,'2025-06-17','2025-06-30'),
    (13,'2025-06-24','2025-07-07'),(14,'2025-07-01','2025-07-14'),
    (15,'2025-07-08','2025-07-21'),(16,'2025-07-15','2025-07-28'),
    (17,'2025-07-22','2025-08-04'),(18,'2025-07-29','2025-08-11'),
    (19,'2025-08-05','2025-08-18'),(20,'2025-08-12','2025-08-25'),
    (21,'2025-08-19','2025-09-01'),(22,'2025-08-26','2025-09-08'),
    (23,'2025-09-02','2025-09-15'),(24,'2025-09-09','2025-09-22'),
]
WINDOW_DATES = {num: (s, e) for num, s, e in WINDOWS}
HIT_EVENTS   = {'single','double','triple','home_run'}
AB_EVENTS    = {'field_out','strikeout','force_out','fielders_choice',
                'grounded_into_double_play','single','double','triple',
                'home_run','fielders_choice_out'}


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_pitches(player_id):
    cache_file = os.path.join(CACHE_DIR, f"{player_id}.json")
    if not os.path.exists(cache_file):
        return None
    with open(cache_file) as f:
        return json.load(f)


def window_woba(pitches, win_num):
    if win_num not in WINDOW_DATES:
        return None
    s, e = WINDOW_DATES[win_num]
    start = datetime.strptime(s, "%Y-%m-%d").date()
    end   = datetime.strptime(e, "%Y-%m-%d").date()
    pa = [p for p in pitches
          if p.get('events') and p.get('woba_denom','0') != '0'
          and start <= datetime.strptime(p['game_date'],"%Y-%m-%d").date() <= end]
    if not pa:
        return None
    return sum(float(p.get('woba_value') or 0) for p in pa) / len(pa)


def avg_woba(pitches, win_nums):
    vals = [window_woba(pitches, n) for n in win_nums]
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


# ── Load player pool ───────────────────────────────────────────────────────────

def load_player_pool():
    """Return list of (name, player_id) for players with cached Statcast data."""
    id_map = {}
    with open(TIMING_FILE, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            id_map[row['name']] = row['id']

    pool = []
    for name, pid in id_map.items():
        if os.path.exists(os.path.join(CACHE_DIR, f"{pid}.json")):
            pool.append((name, pid))

    print(f"Player pool: {len(pool)} players with cached Statcast data")
    return pool


# ── Random sampling ────────────────────────────────────────────────────────────

def run_base_rate(pool):
    """
    For each random sample:
      1. Pick a random player + anchor window
      2. Compute wOBA in that anchor window (must be >= MIN_PRE_WOBA)
      3. Compute wOBA in the 2 windows after
      4. Check if it dropped 30+ points
    This mirrors exactly what we measure in the validation script.
    """
    random.seed(RANDOM_SEED)
    # Windows that have at least 2 windows after them
    eligible_windows = [num for num in WINDOW_DATES if num <= 22]

    outcomes = []
    attempts = 0
    max_attempts = RANDOM_SAMPLES * 10

    print(f"\nSampling {RANDOM_SAMPLES} random player+window combinations...")

    while len(outcomes) < RANDOM_SAMPLES and attempts < max_attempts:
        attempts += 1
        name, pid = random.choice(pool)
        anchor_win = random.choice(eligible_windows)

        pitches = load_pitches(pid)
        if not pitches:
            continue

        pre_woba = avg_woba(pitches, [anchor_win - 1, anchor_win])
        if pre_woba is None or pre_woba < MIN_PRE_WOBA:
            continue   # doesn't meet healthy baseline filter

        after_woba = avg_woba(pitches, [anchor_win + 1, anchor_win + 2])
        if after_woba is None:
            continue

        dropped = after_woba < pre_woba - WOBA_DROP_THRESHOLD
        outcomes.append({
            'name':       name,
            'anchor_win': anchor_win,
            'pre_woba':   pre_woba,
            'after_woba': after_woba,
            'dropped':    dropped,
        })

        if len(outcomes) % 100 == 0:
            print(f"  {len(outcomes)}/{RANDOM_SAMPLES} samples collected...")

    return outcomes


# ── Output ─────────────────────────────────────────────────────────────────────

def write_output(outcomes):
    n        = len(outcomes)
    n_drop   = sum(1 for o in outcomes if o['dropped'])
    base_rate = n_drop / n if n else 0

    # Compare to validation results
    validation_signal = 0.571  # combined rate from validation_2025.py
    lift = validation_signal - base_rate

    lines = [
        "=" * 60,
        "BASE RATE ANALYSIS",
        "Random player+window combinations, 2025 season",
        "=" * 60,
        "",
        "METHODOLOGY",
        f"  Samples drawn         : {n}",
        f"  Baseline filter       : pre-window wOBA ≥ {MIN_PRE_WOBA}",
        f"  Drop threshold        : 30+ wOBA point decline",
        f"  Windows eligible      : 1–22 (need 2 windows after)",
        "",
        "BASE RATE RESULT",
        f"  Random combinations showing 30+pt drop : {n_drop}/{n}",
        f"  Base rate                              : {base_rate*100:.1f}%",
        "",
        "COMPARISON TO VALIDATION",
        f"  Base rate (random)     : {base_rate*100:.1f}%",
        f"  Signal rate (FB arc)   : {validation_signal*100:.1f}%",
        f"  Lift from timing data  : +{lift*100:.1f} percentage points",
        f"  Relative improvement   : {validation_signal/base_rate:.2f}x" if base_rate else "",
        "",
        "INTERPRETATION",
    ]

    if lift > 0.15:
        lines += [
            f"  Strong signal. Fastball late% arcs predict slumps at",
            f"  {lift*100:.1f}pp above the random baseline — meaning the timing",
            f"  data provides meaningful advance warning beyond what you'd",
            f"  expect by chance. This supports publication.",
        ]
    elif lift > 0.08:
        lines += [
            f"  Moderate signal. The {lift*100:.1f}pp lift above baseline is real",
            f"  but modest. Worth publishing with honest framing — the tool",
            f"  improves on chance but is not highly predictive on its own.",
        ]
    else:
        lines += [
            f"  Weak signal. Only {lift*100:.1f}pp above baseline suggests the",
            f"  timing data may be tracking general variability rather than",
            f"  a specific mechanical signal. More investigation needed.",
        ]

    lines += ["", "=" * 60]
    summary = "\n".join(lines)
    print("\n" + summary)

    with open(OUTPUT_FILE, 'w') as f:
        f.write(summary)
    print(f"\nSaved → {OUTPUT_FILE}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not os.path.exists(TIMING_FILE):
        print(f"ERROR: {TIMING_FILE} not found.")
        raise SystemExit(1)
    if not os.path.exists(CACHE_DIR):
        print(f"ERROR: {CACHE_DIR}/ not found. Run validation_2025.py first.")
        raise SystemExit(1)

    pool     = load_player_pool()
    outcomes = run_base_rate(pool)
    write_output(outcomes)
