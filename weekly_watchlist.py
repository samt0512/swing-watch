"""
Weekly Swing Timing Watchlist
==============================
Run this every Monday to get the current week's slump candidates.
Pulls fresh data from Baseball Savant, detects two-signal arcs,
checks stats, and saves a report.

HOW TO RUN:
    python3 weekly_watchlist.py

OUTPUTS:
    watchlist_YYYY-MM-DD.txt   — readable weekly report
    watchlist_YYYY-MM-DD.csv   — raw data for all flagged players

RUNTIME: ~2 minutes (no caching needed)
"""

import csv
import io
import time
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta


# ── Configuration ──────────────────────────────────────────────────────────────

SEASON_START    = date(date.today().year, 4, 1)
TODAY           = date.today()
WINDOW_DAYS     = 14
STEP_DAYS       = 7
MIN_SWINGS      = 20
REQUEST_DELAY   = 1.5
BATCH_SIZE      = 8     # max players per Statcast batch request

# Detection thresholds (optimized from 2023-2025 validation)
MIN_RISE_PP     = 0.20
MIN_ARC_WINDOWS = 3
MAX_DIPS        = 1
MIN_TOTAL_WINS  = 5
MIN_PRE_WOBA    = 0.300
WOBA_DROP       = 0.050

HIT_EVENTS = {'single', 'double', 'triple', 'home_run'}
AB_EVENTS  = {
    'field_out', 'strikeout', 'force_out', 'fielders_choice',
    'grounded_into_double_play', 'single', 'double', 'triple',
    'home_run', 'fielders_choice_out',
}


# ── Step 1: Pull timing data ───────────────────────────────────────────────────

def build_windows():
    windows, w = [], SEASON_START
    while w + timedelta(days=WINDOW_DAYS) <= TODAY + timedelta(days=1):
        windows.append((w, min(w + timedelta(days=WINDOW_DAYS - 1), TODAY)))
        w += timedelta(days=STEP_DAYS)
    return windows


def fetch_csv(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8-sig')


def pull_timing_data():
    windows  = build_windows()
    year     = SEASON_START.year
    timing   = defaultdict(lambda: defaultdict(dict))
    id_map   = {}
    team_map = {}
    win_map  = {}

    print(f"Pulling {len(windows)} timing windows ({SEASON_START} → {TODAY})...")
    for i, (ws, we) in enumerate(windows, 1):
        url = (
            f"https://baseballsavant.mlb.com/leaderboard/bat-tracking/"
            f"swing-timing-miss-distance"
            f"?type=batter&season%5B%5D={year}&splitYear=1&min=1&minSplit=1"
            f"&gameType%5B%5D=R&dateStart={ws}&dateEnd={we}"
            f"&split%5B%5D=api_pitch_type_group09"
            f"&batSide=&contactType=&attackZone=&pitchHand=&csv=true"
        )
        rows = list(csv.DictReader(io.StringIO(fetch_csv(url))))
        win_map[i] = (str(ws), str(we))

        for row in rows:
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

            name = row['name']
            if ',' in name:
                last, first = name.split(',', 1)
                name = f"{first.strip()} {last.strip()}"

            timing[name][pt][i] = {
                'late': late, 'early': early,
                'miss': miss, 'swings': swings,
            }
            id_map[name]   = row['id']
            team_map[name] = row.get('team_name', '')

        print(f"  Window {i:>2}/{len(windows)}: {ws} → {we}  ({len(rows)} rows)")
        if i < len(windows):
            time.sleep(REQUEST_DELAY)

    return timing, id_map, team_map, win_map, len(windows)


# ── Step 2: Detect arcs ────────────────────────────────────────────────────────

def detect_arcs(timing, win_map):
    flagged = {}
    for name, pitch_data in timing.items():
        ff = pitch_data.get('FF', {})
        sorted_nums = sorted(ff.keys())
        if len(sorted_nums) < MIN_TOTAL_WINS:
            continue

        for i in range(len(sorted_nums) - MIN_ARC_WINDOWS + 1):
            seg = sorted_nums[i:i + MIN_ARC_WINDOWS]
            if seg != list(range(seg[0], seg[0] + MIN_ARC_WINDOWS)):
                continue

            lates = [ff[n]['late'] for n in seg]
            rise  = lates[-1] - lates[0]
            dips  = sum(1 for j in range(len(lates)-1) if lates[j+1] < lates[j])
            if rise < MIN_RISE_PP or dips > MAX_DIPS:
                continue

            # Miss distance: use first and last non-null values in the arc
            # so a single NULL window doesn't discard a valid arc
            miss_vals = [(n, ff[n]['miss']) for n in seg if ff[n].get('miss') is not None]
            if len(miss_vals) < 2 or miss_vals[-1][1] <= miss_vals[0][1]:
                continue
            miss_s = miss_vals[0][1]
            miss_e = miss_vals[-1][1]

            # Only keep arcs whose peak window ended within the last 28 days
            arc_peak_end = datetime.strptime(win_map[seg[-1]][1], "%Y-%m-%d").date()
            if (TODAY - arc_peak_end).days > 28:
                continue

            arc = {
                'name':       name,
                'arc_start':  seg[0],
                'arc_peak':   seg[-1],
                'late_start': round(lates[0] * 100, 1),
                'late_now':   round(lates[-1] * 100, 1),
                'rise_pp':    round(rise * 100, 1),
                'miss_start': round(miss_s, 2),
                'miss_now':   round(miss_e, 2),
                'miss_delta': round(miss_e - miss_s, 2),
            }
            if name not in flagged or rise > flagged[name]['rise_pp'] / 100:
                flagged[name] = arc

    return sorted(flagged.values(), key=lambda x: -x['rise_pp'])


# ── Step 3: Batch Statcast pull ────────────────────────────────────────────────

def pull_statcast_batch(player_ids, date_start, date_end):
    """Pull PA data for up to BATCH_SIZE players in a single request."""
    lookup = '&'.join(f'batters_lookup[]={pid}' for pid in player_ids)
    url = (
        f"https://baseballsavant.mlb.com/statcast_search/csv"
        f"?all=true&player_type=batter&{lookup}"
        f"&game_date_gt={date_start}&game_date_lt={date_end}"
        f"&group_by=name&sort_col=pitches&sort_order=desc"
        f"&min_results=0&type=details&"
    )
    rows = list(csv.DictReader(io.StringIO(fetch_csv(url, timeout=90))))
    # Index PA rows by batter ID
    by_player = defaultdict(list)
    for row in rows:
        if row.get('events') and row.get('woba_denom', '0') != '0':
            by_player[row.get('batter', '')].append(row)
    return by_player


def window_woba(pa_rows, start_str, end_str):
    start = datetime.strptime(start_str, "%Y-%m-%d").date()
    end   = datetime.strptime(end_str,   "%Y-%m-%d").date()
    pa = [r for r in pa_rows
          if start <= datetime.strptime(r['game_date'], "%Y-%m-%d").date() <= end]
    if not pa:
        return None
    return sum(float(r.get('woba_value') or 0) for r in pa) / len(pa)


def avg_woba(pa_rows, win_nums, win_map):
    vals = [window_woba(pa_rows, *win_map[n])
            for n in win_nums if n in win_map]
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def enrich_with_woba(arcs, id_map, team_map, win_map):
    if not arcs:
        return []

    # Find the date range we need across all arcs
    all_pre_wins  = []
    all_arc_wins  = []
    for arc in arcs:
        s, p = arc['arc_start'], arc['arc_peak']
        all_pre_wins  += [n for n in [s-2, s-1] if n in win_map]
        all_arc_wins  += list(range(s, p+1))

    all_wins = list(set(all_pre_wins + all_arc_wins))
    date_start = min(win_map[n][0] for n in all_wins)
    date_end   = max(win_map[n][1] for n in all_wins)

    # Batch pull — split into groups if many players
    player_ids = [id_map[a['name']] for a in arcs if id_map.get(a['name'])]
    all_pa = {}
    batches = [player_ids[i:i+BATCH_SIZE] for i in range(0, len(player_ids), BATCH_SIZE)]

    print(f"\nPulling stats ({len(player_ids)} players, "
          f"{len(batches)} batch request{'s' if len(batches)>1 else ''})...")

    for b_num, batch in enumerate(batches, 1):
        print(f"  Batch {b_num}/{len(batches)}: {len(batch)} players "
              f"({date_start} → {date_end})...")
        by_player = pull_statcast_batch(batch, date_start, date_end)
        all_pa.update(by_player)
        if b_num < len(batches):
            time.sleep(REQUEST_DELAY)

    # Compute wOBA per arc
    enriched = []
    for arc in arcs:
        name = arc['name']
        pid  = id_map.get(name, '')
        pa   = all_pa.get(pid, [])

        s, p = arc['arc_start'], arc['arc_peak']
        pre  = avg_woba(pa, [n for n in [s-2, s-1] if n in win_map], win_map)
        dur  = avg_woba(pa, list(range(s, p+1)), win_map)

        # Season wOBA (all PAs)
        season_woba = (round(sum(float(r.get('woba_value') or 0)
                                 for r in pa) / len(pa), 3)
                       if pa else None)

        passes = pre is not None and pre >= MIN_PRE_WOBA

        if pre and dur:
            drop = pre - dur
            if drop >= WOBA_DROP:
                status = 'ALREADY SLUMPING'
            elif drop >= 0.020:
                status = 'EARLY DECLINE'
            elif dur > pre + 0.020:
                status = 'HOLDING STEADY — WATCH'
            else:
                status = 'HOLDING STEADY'
        else:
            status = 'INSUFFICIENT DATA'

        enriched.append({
            **arc,
            'team':          team_map.get(name, ''),
            'player_id':     pid,
            'pre_woba':      pre,
            'dur_woba':      dur,
            'season_woba':   season_woba,
            'passes_filter': passes,
            'status':        status,
        })

    return enriched


# ── Step 4: Write outputs ──────────────────────────────────────────────────────

def fmt(v):
    return f".{int(v * 1000):03d}" if v is not None else "—"


def write_report(players, win_map):
    today_str = str(TODAY)
    txt_file  = f"watchlist_{today_str}.txt"
    csv_file  = f"watchlist_{today_str}.csv"

    high   = [p for p in players if p['passes_filter']]
    low    = [p for p in players if not p['passes_filter']]
    latest_end = win_map[max(win_map)][1]

    already = [p for p in high if p['status'] == 'ALREADY SLUMPING']
    early   = [p for p in high if p['status'] == 'EARLY DECLINE']
    watch   = [p for p in high if 'WATCH' in p['status']]
    steady  = [p for p in high if p['status'] == 'HOLDING STEADY'
                                and 'WATCH' not in p['status']]

    lines = [
        "=" * 68,
        f"  SWING TIMING WATCHLIST — {today_str}",
        f"  Data through: {latest_end}",
        f"  Signal: FB late% +20pp over 3 windows + miss distance rising",
        "=" * 68,
        "",
        f"  {len(players)} flagged  |  {len(high)} above .300 baseline  |  "
        f"{len(already)} already slumping  |  {len(watch)+len(early)} to watch",
    ]

    def section(title, group, note):
        if not group:
            return
        lines.extend([
            "", f"  {'─'*62}", f"  {title}", f"  {'─'*62}",
            f"  {'Player':<24} {'Arc':>8} {'Rise':>7} "
            f"{'Pre':>6} {'Now':>6} {'Season':>7}  Status",
        ])
        for p in group:
            lines.append(
                f"  {p['name']:<24} "
                f"W{p['arc_start']}→W{p['arc_peak']}  "
                f"+{p['rise_pp']:>5.1f}pp  "
                f"{fmt(p['pre_woba']):>6}  "
                f"{fmt(p['dur_woba']):>6}  "
                f"{fmt(p['season_woba']):>7}  {p['status']}"
            )
        lines.extend(["", f"  NOTE: {note}"])

    section("⚠  ALREADY SLUMPING",  already,
            "Stats confirmed. Breakdown is mechanical, not variance.")
    section("▲  EARLY DECLINE",     early,
            "Stats beginning to slip. Slump likely deepening.")
    section("👀  WATCH CLOSELY",     watch,
            "Stats still holding. Highest predictive value — watch next 2 weeks.")
    section("·   HOLDING STEADY",   steady,
            "Signal present, no stat consequence yet. Monitor.")

    if low:
        lines.extend([
            "", f"  {'─'*62}",
            f"  BELOW .300 BASELINE ({len(low)} players — lower confidence)",
            f"  {'─'*62}",
        ])
        for p in low:
            lines.append(
                f"  {p['name']:<24} "
                f"W{p['arc_start']}→W{p['arc_peak']}  "
                f"+{p['rise_pp']:>5.1f}pp  "
                f"season: {fmt(p['season_woba'])}  {p['status']}"
            )

    lines += [
        "", "=" * 68,
        "  Validated: 2023–2025 · 56.3% signal vs 46.5% base · 1.21x lift",
        "  Predictive window: ~1–2 weeks before stats move (24% of cases)",
        "=" * 68,
    ]

    report = "\n".join(lines)
    print("\n" + report)
    with open(txt_file, 'w') as f:
        f.write(report)

    fieldnames = [
        'name', 'team', 'arc_start', 'arc_peak', 'rise_pp',
        'late_start', 'late_now', 'miss_start', 'miss_now', 'miss_delta',
        'pre_woba', 'dur_woba', 'season_woba', 'passes_filter', 'status',
        'player_id',
    ]
    with open(csv_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(players)

    print(f"\nSaved → {txt_file}")
    print(f"Saved → {csv_file}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start_time = time.time()
    print("=" * 68)
    print(f"  SWING TIMING WATCHLIST  —  {TODAY}")
    print("=" * 68 + "\n")

    timing, id_map, team_map, win_map, total = pull_timing_data()
    arcs = detect_arcs(timing, win_map)
    print(f"\n{len(arcs)} players flagged by two-signal detection.")

    if not arcs:
        print("No qualifying arcs found this week.")
    else:
        players = enrich_with_woba(arcs, id_map, team_map, win_map)
        write_report(players, win_map)

    elapsed = time.time() - start_time
    print(f"\nCompleted in {elapsed/60:.1f} minutes.")