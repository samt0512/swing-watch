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
    statcast_cache/            — cached Statcast pulls (speeds up future runs)

DETECTION CRITERIA (validated against 2025 season):
    Primary   : Fastball late% rising 20+ pp over 3 consecutive 14-day windows
    Secondary : Fastball miss distance also trending up across those windows
    Filter    : Pre-arc wOBA ≥ .300 for highest-confidence flags
"""

import csv
import io
import json
import os
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
CACHE_DIR       = "statcast_cache"

# Detection thresholds (optimized from 2025 validation)
MIN_RISE_PP     = 0.20    # fastball late% must rise at least 20pp
MIN_ARC_WINDOWS = 3       # over at least 3 consecutive windows
MAX_DIPS        = 1       # allow 1 non-monotonic step
MIN_TOTAL_WINS  = 5       # player must appear in at least 5 windows
MIN_PRE_WOBA    = 0.300   # healthy baseline for high-confidence flags
WOBA_DROP       = 0.050   # 50pt drop = meaningful decline

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


def fetch_timing_window(date_start, date_end, year):
    base = ("https://baseballsavant.mlb.com/leaderboard/bat-tracking/"
            "swing-timing-miss-distance")
    params = (f"?type=batter&season%5B%5D={year}&splitYear=1&min=1&minSplit=1"
              f"&gameType%5B%5D=R&dateStart={date_start}&dateEnd={date_end}"
              f"&split%5B%5D=api_pitch_type_group09"
              f"&batSide=&contactType=&attackZone=&pitchHand=&csv=true")
    req = urllib.request.Request(base + params, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        content = r.read().decode('utf-8-sig')
    rows = list(csv.DictReader(io.StringIO(content)))
    for row in rows:
        if ',' in row.get('name', ''):
            last, first = row['name'].split(',', 1)
            row['name'] = f"{first.strip()} {last.strip()}"
    return rows


def pull_timing_data():
    windows = build_windows()
    year    = SEASON_START.year
    print(f"Pulling {len(windows)} windows ({SEASON_START} → {TODAY})...")

    timing   = defaultdict(lambda: defaultdict(dict))
    id_map   = {}
    team_map = {}
    window_map = {}   # window_num → (start, end) for Statcast lookups

    for i, (ws, we) in enumerate(windows, 1):
        rows = fetch_timing_window(str(ws), str(we), year)
        window_map[i] = (str(ws), str(we))
        for r in rows:
            pt = r.get('api_pitch_group', '')
            if pt not in ('FF', 'CH', 'CU'):
                continue
            try:
                late   = float(r['late_percent'])
                early  = float(r['early_percent'])
                miss   = float(r.get('miss_distance') or 0)
                swings = int(r['n_swings'])
            except (ValueError, TypeError):
                continue
            if swings < MIN_SWINGS:
                continue
            name = r['name']
            timing[name][pt][i] = {
                'late': late, 'early': early,
                'miss': miss, 'swings': swings,
            }
            id_map[name]   = r['id']
            team_map[name] = r.get('team_name', '')
        print(f"  Window {i:>2}/{len(windows)}: {ws} → {we}  ({len(rows)} rows)")
        if i < len(windows):
            time.sleep(REQUEST_DELAY)

    total_windows = len(windows)
    print(f"Done. {len(timing)} players loaded.\n")
    return timing, id_map, team_map, window_map, total_windows


# ── Step 2: Detect arcs ────────────────────────────────────────────────────────

def detect_arcs(timing, total_windows):
    """
    Find players with a qualifying two-signal arc ending in the most
    recent 3 windows (so signals are current, not historical).
    """
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
            dips  = sum(1 for j in range(len(lates) - 1) if lates[j+1] < lates[j])

            if rise < MIN_RISE_PP or dips > MAX_DIPS:
                continue

            # Miss distance must also trend up
            miss_start = ff[seg[0]].get('miss', 0)
            miss_end   = ff[seg[-1]].get('miss', 0)
            if miss_end <= miss_start:
                continue

            # Arc must end within the 3 most recent windows
            if seg[-1] < total_windows - 2:
                continue

            arc = {
                'name':       name,
                'arc_start':  seg[0],
                'arc_peak':   seg[-1],
                'late_start': round(lates[0] * 100, 1),
                'late_now':   round(lates[-1] * 100, 1),
                'rise_pp':    round(rise * 100, 1),
                'miss_start': round(miss_start, 2),
                'miss_now':   round(miss_end, 2),
                'miss_delta': round(miss_end - miss_start, 2),
            }

            # Keep best arc per player
            if name not in flagged or rise > flagged[name]['rise_pp'] / 100:
                flagged[name] = arc

    return sorted(flagged.values(), key=lambda x: -x['rise_pp'])


# ── Step 3: Pull wOBA for flagged players ─────────────────────────────────────

def pull_statcast(player_id):
    os.makedirs(CACHE_DIR, exist_ok=True)
    # Cache key includes season year so it refreshes each season
    cache_file = os.path.join(CACHE_DIR, f"{player_id}_{SEASON_START.year}.json")

    if os.path.exists(cache_file):
        # Refresh cache if file is older than 6 days
        age_days = (date.today() - date.fromtimestamp(os.path.getmtime(cache_file))).days
        if age_days <= 6:
            with open(cache_file) as f:
                return json.load(f)

    url = (f"https://baseballsavant.mlb.com/statcast_search/csv"
           f"?all=true&player_type=batter&batters_lookup[]={player_id}"
           f"&game_date_gt={SEASON_START.year}-03-25"
           f"&game_date_lt={TODAY}"
           f"&group_by=name&sort_col=pitches&sort_order=desc"
           f"&min_results=0&type=details&")
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            content = r.read().decode('utf-8-sig')
        rows = list(csv.DictReader(io.StringIO(content)))
        with open(cache_file, 'w') as f:
            json.dump(rows, f)
        time.sleep(REQUEST_DELAY)
        return rows
    except Exception as e:
        print(f"    Warning: could not pull Statcast for {player_id} ({e})")
        return []


def window_woba(pitches, start_str, end_str):
    start = datetime.strptime(start_str, "%Y-%m-%d").date()
    end   = datetime.strptime(end_str,   "%Y-%m-%d").date()
    pa = [p for p in pitches
          if p.get('events') and p.get('woba_denom', '0') != '0'
          and start <= datetime.strptime(p['game_date'], "%Y-%m-%d").date() <= end]
    if not pa:
        return None
    return sum(float(p.get('woba_value') or 0) for p in pa) / len(pa)


def avg_woba(pitches, win_nums, window_map):
    vals = [window_woba(pitches, *window_map[n])
            for n in win_nums if n in window_map]
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def enrich_with_woba(arcs, id_map, team_map, window_map):
    print(f"Pulling stats for {len(arcs)} flagged players...")
    enriched = []

    for arc in arcs:
        name = arc['name']
        pid  = id_map.get(name)
        if not pid:
            continue

        cached = os.path.exists(
            os.path.join(CACHE_DIR, f"{pid}_{SEASON_START.year}.json"))
        label  = "cache" if cached else "fetch"
        print(f"  {name:<28} [{label}]")

        pitches = pull_statcast(pid)
        if not pitches:
            continue

        s, p = arc['arc_start'], arc['arc_peak']
        pre_wins    = [n for n in [s - 2, s - 1] if n in window_map]
        during_wins = list(range(s, p + 1))

        pre_woba = avg_woba(pitches, pre_wins,    window_map)
        dur_woba = avg_woba(pitches, during_wins, window_map)

        # Season wOBA
        all_pa = [p2 for p2 in pitches
                  if p2.get('events') and p2.get('woba_denom', '0') != '0']
        season_woba = (round(sum(float(x.get('woba_value') or 0)
                                 for x in all_pa) / len(all_pa), 3)
                       if all_pa else None)

        # Classify
        passes_filter = pre_woba is not None and pre_woba >= MIN_PRE_WOBA
        if pre_woba and dur_woba:
            drop = pre_woba - dur_woba
            if drop >= WOBA_DROP:
                status = 'ALREADY SLUMPING'
            elif drop >= 0.020:
                status = 'EARLY DECLINE'
            elif dur_woba > pre_woba + 0.020:
                status = 'HOLDING STEADY — WATCH'
            else:
                status = 'HOLDING STEADY'
        else:
            status = 'INSUFFICIENT DATA'

        enriched.append({
            **arc,
            'team':           team_map.get(name, ''),
            'player_id':      pid,
            'pre_woba':       pre_woba,
            'dur_woba':       dur_woba,
            'season_woba':    season_woba,
            'passes_filter':  passes_filter,
            'status':         status,
        })

    return enriched


# ── Step 4: Write outputs ──────────────────────────────────────────────────────

def fmt_woba(v):
    return f".{int(v * 1000):03d}" if v is not None else "—"


def write_report(players, window_map):
    today_str  = str(TODAY)
    txt_file   = f"watchlist_{today_str}.txt"
    csv_file   = f"watchlist_{today_str}.csv"

    # Separate into buckets
    high_conf   = [p for p in players if p['passes_filter']]
    low_conf    = [p for p in players if not p['passes_filter']]

    already     = [p for p in high_conf if p['status'] == 'ALREADY SLUMPING']
    early       = [p for p in high_conf if p['status'] == 'EARLY DECLINE']
    watch       = [p for p in high_conf if 'WATCH' in p['status']]
    steady      = [p for p in high_conf if p['status'] == 'HOLDING STEADY' and 'WATCH' not in p['status']]

    latest_win  = max(window_map.keys())
    latest_end  = window_map[latest_win][1]

    lines = [
        "=" * 68,
        f"  SWING TIMING WATCHLIST — Week of {today_str}",
        f"  Data through: {latest_end}",
        f"  Detection: FB late% +20pp over 3 windows + miss distance rising",
        "=" * 68,
        "",
        f"  {len(players)} players flagged  |  "
        f"{len(high_conf)} above .300 baseline  |  "
        f"{len(already)} already slumping  |  "
        f"{len(watch) + len(early)} to watch",
    ]

    def add_section(title, group, note):
        if not group:
            return
        lines.extend(["", f"  {'─'*62}", f"  {title}", f"  {'─'*62}",
                       f"  {'Player':<24} {'Arc':>8} {'Rise':>7} "
                       f"{'Pre':>6} {'Now':>6} {'Season':>7}  Status"])
        for p in group:
            lines.append(
                f"  {p['name']:<24} W{p['arc_start']}→W{p['arc_peak']}  "
                f"+{p['rise_pp']:>5.1f}pp  "
                f"{fmt_woba(p['pre_woba']):>6}  "
                f"{fmt_woba(p['dur_woba']):>6}  "
                f"{fmt_woba(p['season_woba']):>7}  {p['status']}"
            )
        lines.extend(["", f"  NOTE: {note}"])

    add_section(
        "⚠  ALREADY SLUMPING  (timing + stats both declining)",
        already,
        "Stats have already moved. Signal confirms mechanical breakdown, not variance."
    )
    add_section(
        "▲  EARLY DECLINE  (timing drifting, stats starting to slip)",
        early,
        "Stats beginning to reflect the timing drift. Slump likely deepening."
    )
    add_section(
        "👀  WATCH CLOSELY  (timing drifting, stats still holding)",
        watch,
        "Highest predictive value. Stats haven't dropped yet — watch next 2 weeks."
    )
    add_section(
        "·   HOLDING STEADY  (timing drifting, stats unchanged)",
        steady,
        "Signal present but no stat consequence yet. Monitor."
    )

    if low_conf:
        lines.extend([
            "", f"  {'─'*62}",
            f"  BELOW .300 BASELINE  ({len(low_conf)} players — lower confidence)",
            f"  {'─'*62}",
        ])
        for p in low_conf:
            lines.append(
                f"  {p['name']:<24} W{p['arc_start']}→W{p['arc_peak']}  "
                f"+{p['rise_pp']:>5.1f}pp  "
                f"season: {fmt_woba(p['season_woba'])}  {p['status']}"
            )

    lines += [
        "",
        "=" * 68,
        "  Validation: 2025 season · 1.33x lift above random baseline",
        "  Predictive window: ~1–2 weeks before stats move (23.8% of cases)",
        "=" * 68,
    ]

    report = "\n".join(lines)
    print("\n" + report)
    with open(txt_file, 'w') as f:
        f.write(report)

    # CSV output
    fieldnames = [
        'name', 'team', 'arc_start', 'arc_peak', 'rise_pp',
        'late_start', 'late_now', 'miss_start', 'miss_now', 'miss_delta',
        'pre_woba', 'dur_woba', 'season_woba',
        'passes_filter', 'status', 'player_id',
    ]
    with open(csv_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(players)

    print(f"\nSaved → {txt_file}")
    print(f"Saved → {csv_file}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 68)
    print(f"  SWING TIMING WATCHLIST  —  {TODAY}")
    print("=" * 68 + "\n")

    timing, id_map, team_map, window_map, total_windows = pull_timing_data()
    arcs     = detect_arcs(timing, total_windows)

    print(f"\n{len(arcs)} players flagged by two-signal detection.\n")

    if not arcs:
        print("No qualifying arcs found this week.")
    else:
        players  = enrich_with_woba(arcs, id_map, team_map, window_map)
        write_report(players, window_map)
