"""
Multi-Season Validation — 2023 / 2024 / 2025
=============================================
Runs the two-signal arc detection and wOBA validation across all
three available seasons, producing a combined signal rate and base
rate for comparison.

HOW TO RUN:
    python3 validation_multiyear.py

REQUIRES:
    swing_timing_rolling_2023.csv
    swing_timing_rolling_2024.csv
    swing_timing_rolling_2025.csv

OUTPUTS:
    validation_multiyear_results.csv
    validation_multiyear_summary.txt
    statcast_cache/  (cached Statcast pulls — reused across runs)

RUNTIME:
    First run: ~15–20 min (Statcast pulls for arc players)
    Re-runs  : ~2–3 min  (all cached)
"""

import csv
import io
import json
import os
import random
import time
import urllib.request
from collections import defaultdict, Counter
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────────

SEASON_FILES = {
    2023: 'swing_timing_rolling_2023.csv',
    2024: 'swing_timing_rolling_2024.csv',
    2025: 'swing_timing_rolling_2025.csv',
}
CACHE_DIR           = 'statcast_cache'
RESULTS_FILE        = 'validation_multiyear_results.csv'
SUMMARY_FILE        = 'validation_multiyear_summary.txt'

# Detection thresholds (optimized from 2025 parameter sweep)
MIN_RISE_PP         = 0.20
MIN_ARC_WINDOWS     = 3
MAX_DIPS            = 1
MIN_TOTAL_WINDOWS   = 8    # lower for 2023 half-season
MIN_SWINGS          = 20

# Validation thresholds
MIN_PRE_WOBA        = 0.300
WOBA_DROP_THRESHOLD = 0.050
MIN_WINDOWS_AFTER   = 2

# Base rate
N_SAMPLES           = 1000   # larger pool across 3 seasons
RANDOM_SEED         = 42
REQUEST_DELAY       = 1.5

HIT_EVENTS = {'single', 'double', 'triple', 'home_run'}
AB_EVENTS  = {
    'field_out', 'strikeout', 'force_out', 'fielders_choice',
    'grounded_into_double_play', 'single', 'double', 'triple',
    'home_run', 'fielders_choice_out',
}


# ── Step 1: Load timing data (per season) ─────────────────────────────────────

def load_season(year, filepath):
    """
    Returns:
        timing   : {player_name: {pitch_type: {window_num: {late, early, miss}}}}
        id_map   : {player_name: mlbam_id}
        team_map : {player_name: team}
        win_map  : {window_num: (start_date, end_date)}
    """
    timing   = defaultdict(lambda: defaultdict(dict))
    id_map   = {}
    team_map = {}
    win_map  = {}

    with open(filepath, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            pt = row.get('api_pitch_group', '')
            if pt not in ('FF', 'CH', 'CU'):
                continue
            try:
                late   = float(row['late_percent'])
                early  = float(row['early_percent'])
                miss   = row.get('miss_distance', '').strip()
                miss   = float(miss) if miss else None
                swings = int(row['n_swings'])
            except (ValueError, TypeError):
                continue
            if swings < MIN_SWINGS:
                continue

            num  = int(row['window_num'])
            name = row['name']
            timing[name][pt][num] = {
                'late': late, 'early': early,
                'miss': miss, 'swings': swings,
            }
            id_map[name]   = row['id']
            team_map[name] = row.get('team_name', '')
            if num not in win_map:
                win_map[num] = (row['window_start'], row['window_end'])

    return timing, id_map, team_map, win_map


# ── Step 2: Detect arcs ────────────────────────────────────────────────────────

def detect_arcs(timing, win_map, year):
    arcs = []

    for name, pitch_data in timing.items():
        ff = pitch_data.get('FF', {})
        sorted_nums = sorted(ff.keys())
        if len(sorted_nums) < MIN_TOTAL_WINDOWS:
            continue

        best = None
        for i in range(len(sorted_nums) - MIN_ARC_WINDOWS + 1):
            seg = sorted_nums[i:i + MIN_ARC_WINDOWS]
            if seg != list(range(seg[0], seg[0] + MIN_ARC_WINDOWS)):
                continue

            lates = [ff[n]['late'] for n in seg]
            rise  = lates[-1] - lates[0]
            dips  = sum(1 for j in range(len(lates)-1) if lates[j+1] < lates[j])

            if rise < MIN_RISE_PP or dips > MAX_DIPS:
                continue

            # Miss distance must trend up and be non-null at both ends
            miss_start = ff[seg[0]].get('miss')
            miss_end   = ff[seg[-1]].get('miss')
            if miss_start is None or miss_end is None:
                continue
            if miss_end <= miss_start:
                continue

            # Need windows after the arc
            after = [n for n in sorted_nums if n > seg[-1]]
            if len(after) < MIN_WINDOWS_AFTER:
                continue

            arc = {
                'name':       name,
                'year':       year,
                'arc_start':  seg[0],
                'arc_peak':   seg[-1],
                'rise':       rise,
                'late_start': lates[0],
                'late_peak':  lates[-1],
                'miss_start': miss_start,
                'miss_end':   miss_end,
                'after_nums': after[:3],
            }
            if best is None or rise > best['rise']:
                best = arc

        if best:
            arcs.append(best)

    return arcs


# ── Step 3: Statcast pull + cache ─────────────────────────────────────────────

def pull_statcast(player_id, year):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"{player_id}_{year}.json")

    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    url = (f"https://baseballsavant.mlb.com/statcast_search/csv"
           f"?all=true&player_type=batter&batters_lookup[]={player_id}"
           f"&game_date_gt={year}-03-25&game_date_lt={year}-09-30"
           f"&group_by=name&sort_col=pitches&sort_order=desc"
           f"&min_results=0&type=details&")
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = list(csv.DictReader(io.StringIO(r.read().decode('utf-8-sig'))))
        with open(cache_file, 'w') as f:
            json.dump(rows, f)
        time.sleep(REQUEST_DELAY)
        return rows
    except Exception as e:
        print(f"    Warning: pull failed for {player_id}/{year} ({e})")
        return []


# ── Step 4: wOBA computation ───────────────────────────────────────────────────

def window_woba(pitches, start_str, end_str):
    start = datetime.strptime(start_str, "%Y-%m-%d").date()
    end   = datetime.strptime(end_str,   "%Y-%m-%d").date()
    pa = [p for p in pitches
          if p.get('events') and p.get('woba_denom', '0') != '0'
          and start <= datetime.strptime(p['game_date'], "%Y-%m-%d").date() <= end]
    if not pa:
        return None
    return sum(float(p.get('woba_value') or 0) for p in pa) / len(pa)


def avg_woba(pitches, win_nums, win_map):
    vals = [window_woba(pitches, *win_map[n])
            for n in win_nums if n in win_map]
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


# ── Step 5: Validate arcs ─────────────────────────────────────────────────────

def validate_arcs(arcs, id_maps, win_maps):
    results = []
    total   = len(arcs)

    # Pre-pull all unique (player, year) pairs
    seen_pulls = {}
    unique_pairs = list({(a['name'], a['year']) for a in arcs})
    print(f"\nPulling Statcast for {len(unique_pairs)} unique player-seasons...")

    for i, (name, year) in enumerate(unique_pairs, 1):
        pid = id_maps[year].get(name)
        if not pid:
            continue
        cached = os.path.exists(os.path.join(CACHE_DIR, f"{pid}_{year}.json"))
        label  = "cache" if cached else "fetch"
        print(f"  [{i:>3}/{len(unique_pairs)}] {name:<28} {year}  [{label}]")
        seen_pulls[(name, year)] = pull_statcast(pid, year)

    print(f"\nClassifying {total} arcs...")
    for arc in arcs:
        name  = arc['name']
        year  = arc['year']
        s, p  = arc['arc_start'], arc['arc_peak']
        wm    = win_maps[year]
        pitches = seen_pulls.get((name, year), [])

        before_wins = [n for n in [s-2, s-1] if n in wm]
        during_wins = list(range(s, p+1))
        after_wins  = [n for n in arc['after_nums'] if n in wm]

        bef = avg_woba(pitches, before_wins, wm)
        dur = avg_woba(pitches, during_wins, wm)
        aft = avg_woba(pitches, after_wins,  wm)

        passes = bef is not None and bef >= MIN_PRE_WOBA

        if not passes or aft is None:
            lag = 'below_baseline' if not passes else 'insufficient_data'
        else:
            drop_dur = dur is not None and dur < bef - WOBA_DROP_THRESHOLD
            drop_aft = aft < bef - WOBA_DROP_THRESHOLD
            if drop_aft and not drop_dur:
                lag = 'predictive'
            elif drop_dur and drop_aft:
                lag = 'both'
            elif drop_dur and not drop_aft:
                lag = 'concurrent'
            else:
                lag = 'none'

        results.append({
            'name':        name,
            'year':        year,
            'team':        arc.get('team', ''),
            'arc_start':   s,
            'arc_peak':    p,
            'late_start':  round(arc['late_start']*100, 1),
            'late_peak':   round(arc['late_peak']*100, 1),
            'rise_pp':     round(arc['rise']*100, 1),
            'miss_start':  round(arc['miss_start'], 2),
            'miss_end':    round(arc['miss_end'], 2),
            'woba_before': bef,
            'woba_during': dur,
            'woba_after':  aft,
            'passes_filter': passes,
            'lag_pattern': lag,
        })

    return results


# ── Step 6: Base rate ──────────────────────────────────────────────────────────

def compute_base_rate(id_maps, win_maps):
    """Sample random player+window combinations across all seasons."""
    random.seed(RANDOM_SEED)

    # Build pool of (name, year, player_id) from cached players only
    pool = []
    for year in SEASON_FILES:
        for name, pid in id_maps[year].items():
            if os.path.exists(os.path.join(CACHE_DIR, f"{pid}_{year}.json")):
                pool.append((name, pid, year))

    print(f"\nBase rate pool: {len(pool)} cached player-seasons")

    outcomes, attempts = [], 0
    while len(outcomes) < N_SAMPLES and attempts < N_SAMPLES * 10:
        attempts += 1
        name, pid, year = random.choice(pool)
        wm = win_maps[year]
        eligible = [n for n in wm if n + 2 in wm and n - 1 in wm]
        if not eligible:
            continue
        anchor = random.choice(eligible)

        with open(os.path.join(CACHE_DIR, f"{pid}_{year}.json")) as f:
            pitches = json.load(f)

        pre = avg_woba(pitches, [anchor-1, anchor],   wm)
        aft = avg_woba(pitches, [anchor+1, anchor+2], wm)

        if pre is None or pre < MIN_PRE_WOBA or aft is None:
            continue
        outcomes.append(aft < pre - WOBA_DROP_THRESHOLD)

        if len(outcomes) % 200 == 0:
            print(f"  {len(outcomes)}/{N_SAMPLES} samples...")

    br = sum(outcomes) / len(outcomes) if outcomes else 0
    print(f"  Base rate: {br*100:.1f}%  (n={len(outcomes)})")
    return round(br, 3), len(outcomes)


# ── Step 7: Write outputs ──────────────────────────────────────────────────────

def write_outputs(results, base_rate, br_n):
    # CSV
    fieldnames = [
        'name','year','team','arc_start','arc_peak','rise_pp',
        'late_start','late_peak','miss_start','miss_end',
        'woba_before','woba_during','woba_after',
        'passes_filter','lag_pattern',
    ]
    with open(RESULTS_FILE, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(results)

    # Stats
    tested   = [r for r in results if r['passes_filter']]
    excluded = [r for r in results if not r['passes_filter']]
    patterns = Counter(r['lag_pattern'] for r in tested)
    total    = len(tested)

    predictive = patterns.get('predictive', 0)
    concurrent = patterns.get('concurrent', 0)
    both       = patterns.get('both', 0)
    none_      = patterns.get('none', 0)
    insuf      = patterns.get('insufficient_data', 0)
    combined   = (predictive + both) / total if total else 0
    lift       = combined - base_rate

    by_year = defaultdict(lambda: Counter())
    for r in tested:
        by_year[r['year']][r['lag_pattern']] += 1

    # Top predictive cases
    best = sorted(
        [r for r in tested if r['lag_pattern'] == 'predictive'],
        key=lambda x: -((x['woba_before'] or 0) - (x['woba_after'] or 0))
    )[:12]

    lines = [
        "=" * 68,
        "MULTI-SEASON VALIDATION: Fastball Late% + Miss Distance",
        "Seasons: 2023 (half) · 2024 (full) · 2025 (full)",
        "=" * 68,
        "",
        "METHODOLOGY",
        f"  Arc detection   : FB late% +{MIN_RISE_PP*100:.0f}pp + miss dist rising over {MIN_ARC_WINDOWS} windows",
        f"  Baseline filter : pre-arc wOBA ≥ {MIN_PRE_WOBA}",
        f"  Drop threshold  : {WOBA_DROP_THRESHOLD*1000:.0f}+ wOBA point decline",
        "",
        "SAMPLE",
        f"  Total arcs detected       : {len(results)}",
        f"  Excluded (low baseline)   : {len(excluded)}",
        f"  Arcs tested               : {total}",
        "",
        "RESULTS — COMBINED",
        f"  Predictive (lag 1–2 win)  : {predictive:>4}  ({predictive/total*100:.1f}%)" if total else "",
        f"  Concurrent (same window)  : {concurrent:>4}  ({concurrent/total*100:.1f}%)" if total else "",
        f"  Both                      : {both:>4}  ({both/total*100:.1f}%)" if total else "",
        f"  No decline                : {none_:>4}  ({none_/total*100:.1f}%)" if total else "",
        f"  Insufficient data         : {insuf:>4}  ({insuf/total*100:.1f}%)" if total else "",
        "",
        f"  Combined signal rate      : {combined*100:.1f}%  (predictive + both)",
        f"  Base rate                 : {base_rate*100:.1f}%  (n={br_n})",
        f"  Lift                      : {'+' if lift>=0 else ''}{lift*100:.1f}pp",
        f"  Relative improvement      : {combined/base_rate:.2f}x" if base_rate else "",
        "",
        "RESULTS — BY SEASON",
        f"  {'Year':<6} {'Tested':>6} {'Predict':>8} {'Concurr':>8} {'Both':>6} {'None':>6} {'Combined':>9}",
        "  " + "─" * 52,
    ]
    for yr in sorted(by_year.keys()):
        c  = by_year[yr]
        n  = sum(c.values())
        cb = (c['predictive']+c['both'])/n if n else 0
        lines.append(
            f"  {yr:<6} {n:>6}   {c['predictive']:>6}   {c['concurrent']:>6}"
            f"  {c['both']:>5}  {c['none']:>5}   {cb*100:>7.1f}%"
        )

    lines += [
        "",
        "TOP PREDICTIVE CASES",
        f"  {'Player':<22} {'Yr':>4} {'Arc':<16} {'Rise':>7} "
        f"{'Before':>7} {'After':>7} {'Drop':>6}",
        "  " + "─" * 72,
    ]
    for r in best:
        drop = (r['woba_before'] or 0) - (r['woba_after'] or 0)
        arc  = f"W{r['arc_start']}→W{r['arc_peak']}"
        lines.append(
            f"  {r['name']:<22} {r['year']:>4}  {arc:<14} "
            f"+{r['rise_pp']:>5.1f}pp  "
            f".{int((r['woba_before'] or 0)*1000):03d}    "
            f".{int((r['woba_after']  or 0)*1000):03d}   "
            f"−{drop*1000:.0f}pts"
        )

    if lift > 0.12:
        interp = "Strong signal across seasons."
    elif lift > 0.06:
        interp = "Moderate signal. Consistent across seasons."
    elif lift > 0.02:
        interp = "Weak-moderate signal. Further refinement may help."
    else:
        interp = "Weak signal. Consider alternative approaches."

    lines += ["", f"INTERPRETATION: {interp}", "", "=" * 68]

    summary = "\n".join(lines)
    print("\n" + summary)
    with open(SUMMARY_FILE, 'w') as f:
        f.write(summary)
    print(f"\nSaved → {RESULTS_FILE}")
    print(f"Saved → {SUMMARY_FILE}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    for year, path in SEASON_FILES.items():
        if not os.path.exists(path):
            print(f"ERROR: {path} not found.")
            raise SystemExit(1)

    print("=" * 68)
    print("Multi-Season Validation — 2023 / 2024 / 2025")
    print("=" * 68)

    # Load all seasons
    all_timing   = {}
    all_id_maps  = {}
    all_team_maps = {}
    all_win_maps = {}

    for year, path in SEASON_FILES.items():
        print(f"\nLoading {year}...")
        t, i, tm, wm = load_season(year, path)
        all_timing[year]    = t
        all_id_maps[year]   = i
        all_team_maps[year] = tm
        all_win_maps[year]  = wm
        print(f"  {len(t)} players, {len(wm)} windows")

    # Detect arcs per season
    all_arcs = []
    for year in SEASON_FILES:
        arcs = detect_arcs(all_timing[year], all_win_maps[year], year)
        # Attach team info
        for a in arcs:
            a['team'] = all_team_maps[year].get(a['name'], '')
        all_arcs.extend(arcs)
        print(f"  {year}: {len(arcs)} arcs detected")

    print(f"\nTotal arcs across all seasons: {len(all_arcs)}")

    # Validate
    results = validate_arcs(all_arcs, all_id_maps, all_win_maps)

    # Base rate
    base_rate, br_n = compute_base_rate(all_id_maps, all_win_maps)

    # Output
    write_outputs(results, base_rate, br_n)
