"""
Parameter Sweep — Finding the Strongest Signal
===============================================
Tests combinations of arc detection and drop thresholds to find
which parameters produce the largest lift above the random baseline.

All Statcast data is read from statcast_cache/ (no new API calls).
Expect runtime of 2–4 minutes.

HOW TO RUN:
    python3 parameter_sweep.py

REQUIRES:
    - statcast_cache/   (built by validation_2025.py)
    - swing_timing_rolling_2025.csv

OUTPUT:
    parameter_sweep_results.csv   — full results for every combination
    parameter_sweep_summary.txt   — ranked by lift, printed to screen
"""

import csv
import json
import os
import random
from datetime import datetime
from collections import defaultdict
from itertools import product

TIMING_FILE = "swing_timing_rolling_2025.csv"
CACHE_DIR   = "statcast_cache"
RANDOM_SEED = 42
N_SAMPLES   = 500   # random samples per base rate calculation

# Parameter grid to sweep
RISE_THRESHOLDS   = [0.10, 0.15, 0.20, 0.25]   # min pp rise to flag an arc
DROP_THRESHOLDS   = [0.030, 0.050, 0.070]       # min wOBA drop to count as slump
ARC_LENGTHS       = [3, 4]                       # min consecutive windows rising
MIN_PRE_WOBA      = 0.300                        # healthy baseline (fixed)
MAX_DIPS_ALLOWED  = 1                            # allow 1 non-monotonic step (fixed)
MIN_TOTAL_WINDOWS = 10                           # min season coverage (fixed)
MIN_SWINGS        = 20                           # min swings per window (fixed)

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


# ── Data loading ───────────────────────────────────────────────────────────────

def load_timing():
    timing, id_map = defaultdict(dict), {}
    with open(TIMING_FILE, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('api_pitch_group') != 'FF':
                continue
            try:
                late   = float(row['late_percent'])
                swings = int(row['n_swings'])
            except (ValueError, TypeError):
                continue
            if swings < MIN_SWINGS:
                continue
            timing[row['name']][int(row['window_num'])] = late
            id_map[row['name']] = row['id']
    return timing, id_map


def load_all_statcast(id_map):
    """Load every cached player into memory once — avoids re-reading disk per combo."""
    pitches = {}
    for name, pid in id_map.items():
        path = os.path.join(CACHE_DIR, f"{pid}.json")
        if os.path.exists(path):
            with open(path) as f:
                pitches[name] = json.load(f)
    print(f"Loaded Statcast cache for {len(pitches)} players")
    return pitches


# ── wOBA computation ───────────────────────────────────────────────────────────

def window_woba(pitches, win_num):
    if win_num not in WINDOW_DATES:
        return None
    s, e  = WINDOW_DATES[win_num]
    start = datetime.strptime(s, "%Y-%m-%d").date()
    end   = datetime.strptime(e, "%Y-%m-%d").date()
    pa = [p for p in pitches
          if p.get('events') and p.get('woba_denom','0') != '0'
          and start <= datetime.strptime(p['game_date'],"%Y-%m-%d").date() <= end]
    if not pa:
        return None
    return sum(float(p.get('woba_value') or 0) for p in pa) / len(pa)


def avg_woba(pitches, win_nums):
    vals = [window_woba(pitches, n) for n in win_nums if n in WINDOW_DATES]
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


# ── Arc detection ──────────────────────────────────────────────────────────────

def detect_arcs(timing, min_rise, arc_len):
    arcs = []
    for name, wins in timing.items():
        sorted_nums = sorted(wins.keys())
        if len(sorted_nums) < MIN_TOTAL_WINDOWS:
            continue
        best = None
        for i in range(len(sorted_nums) - arc_len):
            seg = sorted_nums[i:i + arc_len]
            if seg != list(range(seg[0], seg[0] + arc_len)):
                continue
            lates = [wins[n] for n in seg]
            rise  = lates[-1] - lates[0]
            dips  = sum(1 for j in range(len(lates)-1) if lates[j+1] < lates[j])
            if rise < min_rise or dips > MAX_DIPS_ALLOWED:
                continue
            after = [n for n in sorted_nums if n > seg[-1]]
            if len(after) < 2:
                continue
            if best is None or rise > best['rise']:
                best = {
                    'name':      name,
                    'arc_start': seg[0],
                    'arc_peak':  seg[-1],
                    'rise':      rise,
                    'after':     after[:3],
                }
        if best:
            arcs.append(best)
    return arcs


# ── Signal rate ────────────────────────────────────────────────────────────────

def signal_rate(arcs, all_pitches, drop_threshold):
    tested = declined = 0
    for arc in arcs:
        pitches = all_pitches.get(arc['name'])
        if not pitches:
            continue
        s, p = arc['arc_start'], arc['arc_peak']
        bef = avg_woba(pitches, [s-2, s-1])
        dur = avg_woba(pitches, list(range(s, p+1)))
        aft = avg_woba(pitches, arc['after'][:2])
        if bef is None or aft is None or bef < MIN_PRE_WOBA:
            continue
        tested += 1
        # combined signal: dropped during OR after
        if dur is not None and dur < bef - drop_threshold:
            declined += 1
        elif aft < bef - drop_threshold:
            declined += 1
    return tested, declined, declined/tested if tested else 0


# ── Base rate ──────────────────────────────────────────────────────────────────

def base_rate(all_pitches, drop_threshold):
    random.seed(RANDOM_SEED)
    pool = list(all_pitches.items())
    eligible_wins = [n for n in WINDOW_DATES if n <= 22]
    outcomes, attempts = [], 0
    while len(outcomes) < N_SAMPLES and attempts < N_SAMPLES * 10:
        attempts += 1
        name, pitches = random.choice(pool)
        anchor = random.choice(eligible_wins)
        pre  = avg_woba(pitches, [anchor-1, anchor])
        if pre is None or pre < MIN_PRE_WOBA:
            continue
        aft = avg_woba(pitches, [anchor+1, anchor+2])
        if aft is None:
            continue
        outcomes.append(aft < pre - drop_threshold)
    n_drop = sum(outcomes)
    return len(outcomes), n_drop, n_drop/len(outcomes) if outcomes else 0


# ── Main sweep ─────────────────────────────────────────────────────────────────

def run_sweep(timing, all_pitches):
    combos = list(product(RISE_THRESHOLDS, DROP_THRESHOLDS, ARC_LENGTHS))
    print(f"Testing {len(combos)} parameter combinations...\n")

    # Pre-compute base rates (only vary by drop threshold)
    print("Computing base rates...")
    base_rates = {}
    for drop in DROP_THRESHOLDS:
        n, n_drop, br = base_rate(all_pitches, drop)
        base_rates[drop] = br
        print(f"  Drop threshold {drop*1000:.0f}pts → base rate {br*100:.1f}%  (n={n})")

    print(f"\nRunning arc detection + signal rate for {len(combos)} combos...")
    results = []
    for rise, drop, arc_len in combos:
        arcs   = detect_arcs(timing, rise, arc_len)
        tested, declined, sig = signal_rate(arcs, all_pitches, drop)
        br     = base_rates[drop]
        lift   = sig - br
        results.append({
            'rise_pp':      int(rise*100),
            'drop_pts':     int(drop*1000),
            'arc_len':      arc_len,
            'n_arcs':       len(arcs),
            'n_tested':     tested,
            'signal_rate':  round(sig*100, 1),
            'base_rate':    round(br*100,  1),
            'lift_pp':      round(lift*100, 1),
            'relative':     round(sig/br, 2) if br else None,
        })

    results.sort(key=lambda x: -x['lift_pp'])
    return results


# ── Output ─────────────────────────────────────────────────────────────────────

def write_output(results):
    # CSV
    with open('parameter_sweep_results.csv', 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)

    # Summary
    lines = [
        "=" * 72,
        "PARAMETER SWEEP — Ranked by Lift Above Base Rate",
        "=" * 72,
        "",
        f"  {'Rise':>6} {'Drop':>6} {'Arc':>4} {'Arcs':>5} {'Tested':>6} "
        f"{'Signal':>7} {'Base':>6} {'Lift':>7} {'Rel':>5}",
        f"  {'(pp)':>6} {'(pts)':>6} {'len':>4} {'':>5} {'':>6} "
        f"{'rate':>7} {'rate':>6} {'(pp)':>7} {'impr':>5}",
        "  " + "─" * 60,
    ]

    for r in results:
        marker = " ◄ BEST" if r == results[0] else ""
        lines.append(
            f"  {r['rise_pp']:>5}pp  {r['drop_pts']:>5}pts  "
            f"{r['arc_len']:>3}w  {r['n_arcs']:>5}  {r['n_tested']:>6}  "
            f"{r['signal_rate']:>6.1f}%  {r['base_rate']:>5.1f}%  "
            f"{'+' if r['lift_pp']>=0 else ''}{r['lift_pp']:>5.1f}pp  "
            f"{r['relative']:.2f}x{marker}"
        )

    top = results[0]
    lines += [
        "",
        "─" * 72,
        "BEST COMBINATION",
        f"  Rise threshold : {top['rise_pp']}pp over {top['arc_len']} consecutive windows",
        f"  Drop threshold : {top['drop_pts']} wOBA points",
        f"  Arcs detected  : {top['n_arcs']} (players with qualifying timing arc)",
        f"  Arcs tested    : {top['n_tested']} (passed healthy baseline filter)",
        f"  Signal rate    : {top['signal_rate']}%",
        f"  Base rate      : {top['base_rate']}%",
        f"  Lift           : +{top['lift_pp']}pp  ({top['relative']}x improvement)",
        "",
        "INTERPRETATION",
    ]

    lift = top['lift_pp']
    if lift > 15:
        lines.append("  Strong signal — publish with confidence.")
    elif lift > 8:
        lines.append("  Moderate signal — publishable with honest framing.")
    elif lift > 4:
        lines.append("  Weak-moderate signal — further refinement recommended.")
    else:
        lines.append("  Weak signal — the metric may need a different approach.")

    lines += ["", "Full results saved → parameter_sweep_results.csv", "=" * 72]

    summary = "\n".join(lines)
    print("\n" + summary)
    with open('parameter_sweep_summary.txt', 'w') as f:
        f.write(summary)
    print("\nSaved → parameter_sweep_summary.txt")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    for f in [TIMING_FILE]:
        if not os.path.exists(f):
            print(f"ERROR: {f} not found.")
            raise SystemExit(1)
    if not os.path.exists(CACHE_DIR):
        print(f"ERROR: {CACHE_DIR}/ not found. Run validation_2025.py first.")
        raise SystemExit(1)

    timing, id_map    = load_timing()
    all_pitches       = load_all_statcast(id_map)
    results           = run_sweep(timing, all_pitches)
    write_output(results)
