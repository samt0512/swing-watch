# swing-watch

A python script that builds a weekly watchlist for slumping hitters.

# Description

Using newly released Baseball Savant bat data and Statcast wOBA, this script creates a watchlist of players with declining swing mechanics that are slumping, could potentially slump soon, or other edge cases(wOBA holding steady/increasing) over the previous month.

# Methodology

The newly released Baseball Savant data includes miss distance and swing timing data for all batters starting in the second half of the 2023 season. Using this data, I built a script that splits the season up into 14-day windows. It then looks at each hitters wOBA, fastball late%, and fastball swing miss distance to create a watchlist of players.

The detection parameters used to define a "slumping" hitter:
  
  Across 3 consecutive 14-day windows, a hitter's:
  * Fastball late% must rise 20+ percentage points
  * Fastball miss distance must be trending up

This data is validated across:
  * 3 seasons(2023(half season), 2024(full season), 2025(full season))
  * 87 arcs(see detection parameters above)

From those validation runs, I found that the baseline for if you picked a random hitter over any 14-day window, their wOBA would drop by 50+ points over the following 2 weeks 46.5% of the time. Then after applying the detection logic, I found that the combined detection signal when hitters met both detection criteria, and their wOBA dropped by 50+ points during, or immediately after the detection window 56.3% of the time. So after looking at those two percentages, I found that the detection criteria identifies slumping hitters 1.2x (+9.9 pp) better than the baseline(random). This signal held up across the 2024(56.8%) and 2025(58.5%) seasons, showing that the findings are not a 1 year fluke.

Then, I wanted to see how good this would be at predicting slumping hitters. I found that in roughly 1 in 4 flagged cases(24.1%), the hitter's wOBA numbers were still holding up during the detection window and only declined in the 1-2 windows afterward. So the script is able to predict a slumping hitter 24.1% of the time. I will continue to monitor this using the tool to see if that holds up in future windows.

# How to Run

## Requirements
Python 3.6 or higher. No additional libraries required.

## Setup
Clone the repository:
```bash
git clone https://github.com/samt0512/swing-watch.git
cd swing-watch
```

## Running the Watchlist
```bash
python3 weekly_watchlist.py
```
The script will pull fresh data from Baseball Savant automatically. Runtime is approximately 2 minutes.

## Running the Validation Scripts
To verify the methodology, download the three season CSVs from the `data/` folder and place them in the same directory as the scripts, then run in this order:

1. `python3 validation_multiyear.py` — builds the statcast_cache/ folder and runs the full validation. **Note: first run takes 15-20 minutes** as it pulls Statcast data for every qualifying player. Subsequent runs are fast due to caching.
2. `python3 parameter_sweep.py` — shows how the detection thresholds were optimized
3. `python3 multi_signal_sweep.py` — shows how the two-signal combination was selected
4. `python3 base_rate.py` — verifies the baseline comparison

# Output example

The output for the script comes in the form of a .txt file. Ex. watchlist_2026_06_11.txt.

It contains 3 sections:

⚠  ALREADY SLUMPING
- These are players whose fastball late % increased and are already seeing a major decline in wOBA.

👀  WATCH CLOSELY
- These are players who are seeing a significant increase in fastball late % but their wOBA is holding steady for now and have a high predictive value.

BELOW .300 BASELINE
- These are players whose fastball late % increased but their wOBA started below the .300 baseline so there is low confidence in their predictive value.

Within these 3 sections, each includes:
- Player column: player name
- Arc: the windows across which their fastball late % increased
- Rise: how much the players fastball late % increased
- Pre: the player's wOBA before the arc.
- Now: the player's wOBA during this window.
- Season: the player's season to date wOBA.
- Status: the player's current status based on how their wOBA has responded to the timing drift.

```
====================================================================
  SWING TIMING WATCHLIST — 2026-06-11
  Data through: 2026-06-09
  Signal: FB late% +20pp over 3 windows + miss distance rising
====================================================================

  16 flagged  |  7 above .300 baseline  |  5 already slumping  |  2 to watch

  ──────────────────────────────────────────────────────────────
  ⚠  ALREADY SLUMPING
  ──────────────────────────────────────────────────────────────
  Player                        Arc    Rise    Pre    Now  Season  Status
  Adolis García            W5→W7  + 36.7pp    .332    .223     .260  ALREADY SLUMPING
  Hyeseong Kim             W4→W6  + 31.0pp    .356    .274     .283  ALREADY SLUMPING
  Alec Bohm                W7→W9  + 28.6pp    .448    .308     .302  ALREADY SLUMPING
  Nathan Church            W5→W7  + 25.6pp    .399    .264     .337  ALREADY SLUMPING
  Miguel Andujar           W7→W9  + 20.0pp    .339    .217     .298  ALREADY SLUMPING

  NOTE: Stats confirmed. Breakdown is mechanical, not variance.

  ──────────────────────────────────────────────────────────────
  👀  WATCH CLOSELY
  ──────────────────────────────────────────────────────────────
  Player                        Arc    Rise    Pre    Now  Season  Status
  Spencer Horwitz          W5→W7  + 25.4pp    .345    .431     .391  HOLDING STEADY — WATCH
  Sam Antonacci            W4→W6  + 24.1pp    .332    .396     .370  HOLDING STEADY — WATCH

  NOTE: Stats still holding. Highest predictive value — watch next 2 weeks.

  ──────────────────────────────────────────────────────────────
  BELOW .300 BASELINE (9 players — lower confidence)
  ──────────────────────────────────────────────────────────────
  Tyrone Taylor            W5→W7  + 31.7pp  season: .241  ALREADY SLUMPING
  Garrett Mitchell         W6→W8  + 30.4pp  season: .325  HOLDING STEADY — WATCH
  Joey Ortiz               W6→W8  + 30.0pp  season: .264  HOLDING STEADY — WATCH
  Victor Scott II          W4→W6  + 25.5pp  season: .261  HOLDING STEADY — WATCH
  Carter Jensen            W7→W9  + 24.8pp  season: .278  EARLY DECLINE
  Jakob Marsee             W6→W8  + 22.2pp  season: .320  HOLDING STEADY — WATCH
  Cedric Mullins           W7→W9  + 21.8pp  season: .298  HOLDING STEADY — WATCH
  Christian Vázquez        W7→W9  + 20.9pp  season: .276  HOLDING STEADY — WATCH
  Will Smith               W7→W9  + 20.6pp  season: .331  HOLDING STEADY — WATCH

====================================================================
  Validated: 2023–2025 · 56.3% signal vs 46.5% base · 1.21x lift
  Predictive window: ~1–2 weeks before stats move (24% of cases)
====================================================================
```
