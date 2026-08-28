# MLB Blowout Bot — Full Spec & Dashboard Brief

Current as of **2026-08-26**. This is the complete state of the system, written so it can be handed to a design tool or a fresh chat with no other context.

**Live:** https://web-production-586f7.up.railway.app/
**Repo:** `github.com/nickwells21/MLB-Blowout-Bot` (Railway auto-deploys from `main`)
**Local:** `C:\Users\nickw\Claudes Folder\MLB-Blowout-Bot`

If you are here to redesign the dashboard, jump to **§5 Data Contracts** and **§6 What the Dashboard Must Show**. Everything above that is context for why the data looks the way it does.

---

## 1. The Betting Thesis

When a losing MLB team sends a **position player to pitch** during a blowout, they have publicly conceded. The bet: the winning team's run line should move further in their favor from that moment, so you buy the winning side's alternate spread ladder the instant it happens.

The bot **detects and alerts only**. It never places a bet.

**Still unvalidated:** there is no backtest. We know the mechanism fires; we do not know whether the spread actually moves enough, often enough, to beat the juice. Treat every alert as a research signal, not a confirmed edge.

---

## 2. Alert Tiers

Six distinct notification types, all pushed via ntfy.sh to topic `MLB_Bets-Blowout-Bot-7x2q`.

| Tier | Fires when | Priority | Repeats? |
|---|---|---|---|
| **Big Lead Watch** | Lead reaches 6+, any inning | default | Every +3 runs (6, 9) — suppressed at 10+ |
| **Extreme Lead** | Lead reaches 10+ | urgent | Every +2 runs (10, 12, 14, 16…) |
| **Urgency Mode ON/OFF** | Lead 6+ **AND** trailing team used 2+ relievers | urgent | Once per entry; exits below lead 4 |
| **Inning Report** | Half-inning flips — **only while in Urgency Mode**. One consolidated push: inning + current lead (and its change since the last report) + trailing bullpen state | high | Every Top/Bottom transition |
| **Pitcher Change** | New reliever for the trailing team, once a game is flagged | urgent | Every substitution |
| **Bullpen Exhausted** | Trailing team has used **3** relievers — no lead or inning gate | high | Once per game per side |
| **Bullpen Exhausted — URGENT** | Trailing team has used **4** relievers | urgent | Once per game per side |
| **GOLDEN — Position Player** | A field player takes the mound. **Always fires, overrides every rule above** | urgent | Once per player |

There is no hourly summary and no "Bot Started" ping. Both were removed: they
were volume between the alerts that matter, and the startup ping fired on every
redeploy.

**Priority is set so the routine push is the quiet one.** The Inning Report
sends at ntfy `low` — silent, for a game you are already watching. Big Lead
(`default`), Bullpen Exhausted (`high`/`urgent`) and GOLDEN (`urgent`) all
outrank it, by design.

**Tier interaction rules that matter:**
- Big Lead self-suppresses at 10+ so Extreme owns that range — no double-firing at lead 12.
- Pitcher Change alerts pick their label from the strongest active state: **urgency > extreme > big lead**. One push per substitution, never one per matching tier.
- The Urgency Mode entry message already names the incoming pitcher and current inning, so the Pitcher Change and Inning Report pushes are suppressed on that exact poll.
- If the new arm is a *position player*, both Pitcher Change and the Bullpen ladder stay silent and let the GOLDEN tier own the moment.
- A bullpen rung suppresses **Pitcher Change** but **not** the Inning Report: both go out, and because the rung outranks the report it is sent last and sits directly above it. Urgency Mode *entry* still suppresses the report, since that message already is a full report and fires once per game.
- The bullpen ladder is keyed per side, so a lead change starts a fresh ladder for the newly trailing team.
- Big Lead Watch folds into the **Urgency Mode ON** message only. It no longer folds into the Inning Report — folding there would demote a Big Lead to the report's `low` priority, and it is one of the two alerts that must outrank it. Extreme Lead never folds. Mid-inning, both lead tiers push immediately on their own.
- **A restart never replays the slate.** The first poll after the process starts adopts whatever is already in progress without alerting; only GOLDEN still fires. See `AlertBus.flush()`.

### Send order is the real priority system

A phone stack shows the newest push on top, so **the alert sent last is the one you see**. Order used to be an accident of the order `check_game()` called the tiers — which put the Inning Change ping on top of the Bullpen Exhausted ping (user-reported), and put GOLDEN, which correctly runs first, at the very bottom.

Tiers now queue into an `AlertBus` and the poll's batch is sent in **ascending importance**:

| Rank | Alert |
|---|---|
| 10 | Urgency Mode Off |
| 20 | Inning Report |
| 30 | Big Lead Watch |
| 40 | Extreme Lead |
| 50 | Urgency Mode ON |
| 60 | Pitcher Change |
| 70 | Bullpen Exhausted |
| 80 | Bullpen Exhausted — URGENT |
| **100** | **GOLDEN — Position Player** |

GOLDEN is **computed first** (nothing that can fail runs before it) and **sent last** (nothing can cover it). The flush is in a `finally` and each send is isolated, so neither a tier escaping `_safe()` nor a rate-limited push drops the golden alert queued behind it. A tier added without a rank sorts below everything, so it can never bury a critical alert.

### Urgency Mode is a latched state, not an alert

This is the only tier with real state. A game **enters** at lead 6+ with 2+ relievers used, and **exits** below lead 4 (or on a tie). The 6-in/4-out gap is hysteresis — without it a game hovering at 5–6 runs would flip in and out and spam transitions.

While latched, you get one Inning Report per half-inning plus a push on **every trailing-team pitcher change**. Expect **8–12 notifications** from a game that enters in the 5th and runs to the 9th — fewer than before, since the lead update now rides along with the inning report instead of being its own push. Entry state persists across bot restarts.

---

## 3. Game-Window Scheduling

The bot does **not** run 24/7. Each cycle it computes the current Eastern-time day's window:

```
sleep ──► wake 5 min before first pitch ──► poll every 30s ──► all games Final ──► sleep
                                                                                    │
                                                     next day that has games ◄──────┘
```

While asleep it still rewrites `status.json` every 60s, so the dashboard can tell **deliberate sleep** from a **crashed process**. It also blanks `live_snapshot.json` so yesterday's finished games don't sit frozen on screen.

### Four failure modes this survives — all found against real API data

1. **Day boundaries are Eastern, not UTC.** A game starting `01:05Z` carries the *previous* day's `officialDate` and belongs to that slate. Filtering any other way sleeps through late west-coast games. (This exact bug shipped once already.)
2. **Postponed/Cancelled games report `abstractGameState: "Final"`.** Only `detailedState` distinguishes them. A naive all-Final check would call the slate complete and sleep while games were still scheduled.
3. **The entire postseason is scheduled at `07:33Z` with placeholder names** like "AL Higher Seed" until matchups are set. Obeying that would wake the bot at 3:33 AM and put it back to sleep hours before the real 8 PM first pitch. The bot treats any first pitch inside **03:00–14:00Z** — a band no real MLB game starts in — as untrustworthy and polls straight through that day.
4. **An API outage must never read as "no games."** `compute_window` raises on a cold-cache failure and the loop keeps polling rather than going to sleep.

**Known gap:** a suspended game resumed the next day is counted once on its original date; its backstop expires before the resumption plays. Nothing is missed in practice because `get_live_game_pks` still sees it live, but the scheduler doesn't know the resumption is coming.

**Season cache:** 494 games across 61 playing days (Aug 26 → Nov 30) in `season_schedule.json`, refreshed on startup when >24h old. This is **reference only** — live behavior always queries the current day fresh, because a static file goes silently wrong on rainouts and reschedules.

---

## 4. Architecture

| File | Role |
|---|---|
| `bot.py` | All alert tiers + the scheduler state machine (`run_loop` → `run_scheduled` / `run_unscheduled`) |
| `schedule.py` | `today_et()`, `get_day_games()`, `compute_window()`, `fetch_season()`. TTL-cached ~5 min |
| `mlb_api.py` | MLB Stats API wrapper (no key). Live games, boxscores, bullpen detail, position-player detection |
| `odds.py` | The Odds API. Snapshot on a slow cadence + one fresh fetch at alert time to lock the line |
| `notifier.py` | ntfy.sh push (title, message, priority, tags) |
| `state.py` / `alert_log.py` | Dedupe across restarts / alert history (capped 500) |
| `app.py` | Flask entrypoint — runs the loop in a thread, serves the dashboard + JSON |
| `dashboard.html` | The UI. Vanilla JS, no build step, 15s refresh |

**Stack:** Python, Flask, `requests`. That's it — no database, no ORM, no frontend framework. State lives in JSON files under `DATA_DIR` (a Railway Volume).

---

## 5. Data Contracts

The dashboard is a **static HTML file that polls three JSON endpoints every 15 seconds**. Any redesign consumes exactly these. All are served from the app root.

### `GET /status.json`

```json
{
  "last_checked": "2026-08-26T17:15:32.108Z",
  "live_games": 1,
  "bot_state": "scanning",
  "schedule": {
    "date": "2026-08-26",
    "games_today": 15,
    "games_final": 0,
    "first_pitch": "2026-08-26T17:10:00+00:00",
    "last_scheduled_start": "2026-08-27T01:05:00+00:00",
    "next_wake": null
  },
  "run_diff_mid": 6,
  "run_diff_late": 4,
  "bullpen_exhaustion_count": 3,
  "bullpen_critical_count": 4,
  "big_lead_threshold": 6,
  "big_lead_step": 3,
  "extreme_lead_threshold": 10,
  "extreme_lead_step": 2,
  "urgency_run_diff": 6,
  "urgency_min_relievers": 2,
  "urgency_exit_run_diff": 4,
  "poll_interval_seconds": 30
}
```

- `bot_state` is `"scanning"` or `"sleeping"`.
- `next_wake` is non-null **only while sleeping**.
- **`schedule` and `bot_state` may be absent entirely** on an older deploy. Render correctly and never throw when they are.

### `GET /live_snapshot.json`

```json
{
  "fetched_at": 1756228531.4,
  "book": "draftkings",
  "quota": { "remaining": "19338", "used": "662" },
  "games": [
    {
      "game_pk": 824948,
      "away_team": "Tampa Bay Rays",
      "home_team": "Detroit Tigers",
      "away_runs": 0,
      "home_runs": 0,
      "inning": 1,
      "inning_ordinal": "1st",
      "inning_state": "Top",
      "balls": 0, "strikes": 1, "outs": 1,
      "urgency": false,
      "away_bullpen": [ /* see below */ ],
      "home_bullpen": [
        {
          "player_id": 675512,
          "name": "Troy Melton",
          "position": "P",
          "is_position_player": false,
          "innings_pitched": "2.0",
          "pitches": 30,
          "strikes": 19,
          "balls": 11,
          "strike_pct": 63,
          "hits": 2,
          "runs": 1,
          "earned_runs": 1,
          "walks": 1,
          "strikeouts": 3,
          "home_runs": 0,
          "batters_faced": 8,
          "outs": 6,
          "era": "4.15",
          "note": "(W, 9-2)",
          "is_starter": true,
          "is_current": true
        }
      ],
      "odds": {
        "book": "draftkings",
        "book_title": "DraftKings",
        "home_team": "Detroit Tigers",
        "away_team": "Tampa Bay Rays",
        "moneyline_away": 129,
        "moneyline_home": -149,
        "spreads_away": [ { "point": 5.5, "price": -1750 }, "… up to 18 rungs" ],
        "spreads_home": [ { "point": -5.5, "price": 850 }, "…" ],
        "total_point": 6.5,
        "total_over_price": -144,
        "total_under_price": 111
      }
    }
  ]
}
```

Notes for anyone rendering this:
- `fetched_at` is an **epoch float**, unlike `last_checked` which is an ISO string.
- `odds` is **`null`** whenever the book doesn't post that matchup or the snapshot hasn't refreshed. Always guard it.
- `inning_state` is `"Top"`, `"Middle"`, `"Bottom"`, or `"End"`.
- Bullpen arrays are **chronological** — starter first, current pitcher last. Only the first entry has `is_starter: true` and only the last has `is_current: true`. MLB writes final pitch counts on exit, so earlier entries freeze on their own.
- `innings_pitched` is MLB notation: `"3.1"` means 3⅓ innings, **not** 3.1.
- **Every per-pitcher stat is free.** `strikes` / `balls` / `hits` / `runs` / `earned_runs` / `walks` / `strikeouts` / `home_runs` / `batters_faced` / `outs` all come out of the boxscore the bot already fetches on every poll, `era` out of that same payload's `seasonStats`, and `strike_pct` is computed locally as `round(100 * strikes / pitches)`. The fuller pitcher line costs **zero extra API requests** and does not touch polling frequency — do not add a separate fetch for any of it.
- `era` is a **string** (`"4.15"`, or `"-.--"` / `"INF"` for a pitcher with no season innings), not a number. `note` is MLB's own decision string (`"(W, 9-2)"`, `"(H, 12)"`) and is usually absent.
- **Any of these can be `null` or missing** — a pitcher who has just entered has no line yet, and `strike_pct` is `null` whenever `pitches` is 0 or absent. Render an em dash, never `NaN`.
- Spread ladders arrive unfiltered and unsorted, up to 18 rungs including unbettable lock lines. The existing dashboard filters to the side matching the moneyline sign, drops anything outside ±600, sorts by absolute point, and caps at 8.

### `GET /alerts_log.json`

Array, oldest first, capped at 500. Render newest first.

```json
[
  {
    "timestamp": "2026-08-25T02:14:07+00:00",
    "game_pk": 824901,
    "tier": "bullpen_exhausted",
    "inning": 8,
    "outs": 1,
    "prior_relievers_used": 5,
    "player_name": "Nick Allen",
    "player_position": "Shortstop",
    "conceding_team": "Athletics",
    "conceding_team_runs": 2,
    "bet_team": "New York Yankees",
    "bet_team_runs": 14,
    "run_diff": 12,
    "odds_at_alert": {
      "book_title": "DraftKings",
      "home_team": "Athletics",
      "away_team": "New York Yankees",
      "spread_away": -8.5,
      "spread_away_price": 145,
      "ml_away": -2200
    }
  }
]
```

`tier` is `"blowout"` or `"bullpen_exhausted"`. **`odds_at_alert` uses different key names than the live snapshot** — `spread_away` / `ml_away` (singular, side-suffixed), not `spreads_away` / `moneyline_away`. This is a real inconsistency; don't assume one shape works for both.

---

## 6. What the Dashboard Must Show

Anything replacing `dashboard.html` has to carry all of this. Grouped by priority.

### Must have

1. **Bot state banner** — scanning vs sleeping. When sleeping: a live countdown to `next_wake` and the wake time in the viewer's local timezone. **Hide entirely** if `bot_state` is absent rather than guessing. This is the difference between "idle on purpose" and "dead," and it is the first thing to look at.
2. **Live game cards** — matchup, score with the leading side visually dominant, inning + half, ball-strike-out count.
3. **Urgency flag** — games with `urgency: true` must be unmistakable and sorted to the top.
4. **Run-line ladder per side** — the actual bet. Filter as described in §5.
5. **Bullpen order per team** — name plus the full outing line (IP, pitches, strike %, H, R, ER, BB, K), season ERA, decision, current pitcher marked, position players marked louder still. This is the leading indicator that a position player is coming. The stat line is what turns a list of names into a readable story about *why* the manager keeps going back to the pen, and it is free — see §5, it rides along in the boxscore already fetched each poll.
   - **Colour convention: red is damage the pitcher allowed, green is damage he avoided.** Reading straight down the trailing team's column, a wall of red is the bullpen coming apart — the exact state a position player emerges from. Keep header and stat rows on one shared grid template so the digits line up as true columns; the reading-down scan is the whole point. A position-player row outranks the amber "now" treatment and goes red.
6. **Alert history** — tier, teams, score, run diff, the player who pitched, and the line captured at alert time.
7. **Freshness** — `last_checked` age. Stale data on a betting dashboard is worse than no data.

### Should have

8. Moneyline and game total per matchup
9. Games today / games final progress against the slate
10. First pitch time, local
11. Odds API quota remaining (free tier is finite; blowing through it kills the odds display)
12. Sort and filter — by lead, inning, bullpen depth; filter to urgency-only

### Design constraints

- **Dark theme.** Current palette: `#0b0d12` ground, `#141821` panels, `#262b36` borders, `#f2a71b` accent, `#2fbf71` good, `#e6584f` bad.
- **Vanilla JS, no build step.** Served as a static file by Flask.
- **15s auto-refresh**, no manual reload.
- **Scannable at a glance.** The real use case is a phone or second monitor while games run — the answer to "is anything about to happen?" should take under two seconds.
- **Never throw on missing data.** `odds` is routinely null, `schedule` may be absent, bullpens may be empty. Every one of these is normal, not an error.
- Escape all JSON-derived strings before inserting into HTML.

### Known weak points in the current UI

- One card per row wastes horizontal space; a full slate means heavy scrolling.
- No sort or filter at all — 15 games render in whatever order the API returned.
- Alert history is a flat list with no grouping, filtering, or outcome tracking.
- Mobile layout is untested.
- A tried-and-rejected 4-up gallery redesign is in git history as `8d1bfc2` (reverted by `5fd0762`) — **the density loss was the problem**, ladders and bullpens got pushed behind a click. Worth reading before proposing anything similar.

---

## 7. Open Questions

Real gaps, roughly in order of value:

1. **No backtest.** The core thesis is unproven. How often does this fire per season, and does the line actually move? Kill or scale the project on that answer.
2. **No post-alert line tracking.** We snapshot the line at alert time and never look again — so we can't measure the thesis even going forward. Polling odds for 15 min after each alert would make the bot self-measuring.
3. **No outcome recording.** No way to mark an alert win/loss/push, so no ROI.
4. **JSON files, not a database.** Can't query "alerts by team, by run diff, by month." SQLite would fix this and enable everything above.
5. **Single book.** DraftKings only, no line shopping at alert time.
6. **No suggested stake.** Alerts say what happened, not what to bet or how much.

---

## 8. Running It

```bash
cd "C:/Users/nickw/Claudes Folder/MLB-Blowout-Bot"
pip install -r requirements.txt
cp .env.example .env    # set NTFY_TOPIC, ODDS_API_KEY
python app.py           # bot + dashboard at http://localhost:8090
```

`python bot.py` runs the alerting loop with no web server.

Deploy by pushing to `main`. Key env vars: `SCHEDULE_ENABLED` (false = 24/7), `POLL_INTERVAL_SECONDS`, `URGENCY_RUN_DIFF`, `EXTREME_LEAD_THRESHOLD`, `ODDS_BOOK`. Full list in `.env.example`.

---

## 9. Paste This Into a Fresh Chat

> I have a Python bot on Railway that watches live MLB games and pushes phone alerts when a losing team's situation suggests they've conceded — the betting thesis is that the winning team's run line moves further in their favor at that moment. It runs six alert tiers including a latched "urgency mode," and it only runs during actual game windows (sleeps overnight, wakes at first pitch). It serves a vanilla-JS dashboard off three JSON endpoints. I want to redesign that dashboard. The full spec, data contracts, and current UI weak points are in the doc I'm attaching.
