# MLB Blowout Bot

**Purpose:** Watches live MLB games and alerts (push notification) when a losing team brings in a position player to pitch during a blowout. Betting angle: bet the winning team's spread to increase from that point, since the position player appearance signals the losing team has conceded.

**Setup:**
```bash
cd "C:/Users/nickw/Claudes Folder/MLB-Blowout-Bot"
pip install -r requirements.txt
cp .env.example .env
# Set NTFY_TOPIC in .env to something unique (e.g. nick-mlb-blowout-7x2q),
# then install the ntfy app (iOS/Android) and subscribe to that same topic.
# Set ODDS_API_KEY (from the-odds-api.com, free tier 500 credits/mo) to enable
# live spread/moneyline/total display on the dashboard. Optional — the bot
# still detects and alerts without it, just without odds attached.
```

**Run locally (standalone, no web server):**
```bash
python bot.py
```

**Run as a web service (bot + dashboard together, used for deployment):**
```bash
python app.py
# Open http://localhost:8090
```
Either way it polls live games every `POLL_INTERVAL_SECONDS` (default 30) and pushes an alert the moment a position player takes the mound in a game that's a blowout by `BLOWOUT_RUN_DIFF` runs (default 6). `app.py` runs this same loop in a background thread and serves the dashboard over HTTP — this is the version deployed to Railway (see `Procfile`).

**Rule hierarchy — read this before touching the tiers.**

There is one signal that matters and five that support it.

*TIER 0 — GOLDEN.* A position player is pitching. This is the entire premise
of the bot. It **overrides every other rule and always fires** — `_classify`
never returns None; the tiers below it (`bullpen_exhausted`, `blowout`,
`position_player`) only grade how strong the betting case is. Two rules protect
it structurally: it runs **first** in `check_game`, before anything that can
fail, and needs nothing but the boxscore. Everything it touches afterwards
(odds, alert log) is wrapped in `_safe()` so a dependency failure degrades the
message instead of losing the push. Never move this below the context tiers,
and never add a gate that can return no alert.

*TIER 1 — CONTEXT.* Big Lead Watch, Extreme Lead, Urgency Mode, Inning Change,
Pitcher Change. These **do not gate Tier 0**. They exist to flag games where a
position player is becoming likely, so you are already watching when it
happens. Each runs through `_safe()` so one failing tier cannot suppress the
others — a rate-limited ntfy push used to abort the whole game's checks,
including the golden one.

`notifier.send_alert` catches `Exception`, not just `RequestException`, for the
same reason: it is called from inside the golden path.

**Detection depends on roster position, never in-game position.** See
`mlb_api.find_position_players_pitching` — the boxscore relabels a position
player as "P" the moment they take the mound, so the boxscore field is useless
for this exact question. `player_positions.json` holds the league-wide record
(~1,413 players), swept once a day.

**Game-window scheduling:** The bot does not run 24/7. Each cycle it asks
`schedule.py` for the current Eastern-time day's window, then sleeps until ~5
minutes before first pitch, polls every `POLL_INTERVAL_SECONDS` until every game
on the slate is Final, and sleeps again until the next day that has games.
While asleep it still rewrites `status.json` every `SLEEP_HEARTBEAT_SECONDS` so
the dashboard can distinguish deliberate sleep from a crashed process. Set
`SCHEDULE_ENABLED=false` to go back to 24/7 polling.

Three things the scheduler has to get right, all of which bit us:
- **Day boundaries are Eastern, not UTC.** A game starting `01:05Z` has
  `officialDate` of the *previous* day and belongs to that slate. Filtering on
  anything but `officialDate` makes the bot sleep through late west-coast games.
- **Postponed/Cancelled games report `abstractGameState: "Final"`.** Only
  `detailedState` distinguishes them. `compute_window` excludes them from both
  the game count and the all-Final check; trust its `is_postponed` flag.
- **Postseason games are placeholders until matchups are set** — every one is
  scheduled at `07:33Z` with names like "AL Higher Seed". `bot.py` detects a
  first pitch inside `PLACEHOLDER_START_HOURS` (03:00–14:00Z, a band no real MLB
  game starts in) and polls straight through that day instead of trusting the
  window. Without this the bot would wake at 3:33am, find nothing, and sleep
  through the actual 8pm games.

**Architecture:**
- `mlb_api.py` — wraps the free MLB Stats API (statsapi.mlb.com, no key required): live game lookup, boxscore fetch, and the core detection logic (a player who appears in a team's `pitchers` list but whose real position isn't "P"/"TWP" is a position player pitching).
- `schedule.py` — the day/season schedule. `today_et()`, `get_day_games(date)`,
  `compute_window(date)` (first pitch, last start, backstop, all-Final state),
  and `fetch_season()` which pulls the remaining season in one ranged request
  and caches it to `season_schedule.json` for reference. Day lookups are
  TTL-cached (~5 min) so the poll loop doesn't hammer the API. On an API outage
  with a cold cache it raises rather than reporting an empty slate — an outage
  must never read as "no games, go to sleep".
- `odds.py` — wraps The Odds API for live spread/moneyline/total data. `maybe_refresh_snapshot()` writes a dashboard-wide snapshot (`odds_snapshot.json`) on a slow cadence (`ODDS_REFRESH_SECONDS`); `fetch_for_alert()` does one fresh fetch at alert time so the recorded line matches the moment the position player enters. No-ops entirely if `ODDS_API_KEY` is unset.
- `bot.py` — the alert tiers plus the scheduler state machine (`run_loop` →
  `run_scheduled` / `run_unscheduled`), blowout-threshold checks, alert
  composition (attaches the current line from `odds.py` when available), and the
  `status.json` heartbeat written both while polling and while asleep.
- `notifier.py` — pushes to ntfy.sh (free, no signup). Swap in Pushover/Twilio here if you want SMS instead.
- `state.py` — persists which events have already been alerted on (`state.json`) so restarts/re-polls don't spam duplicates.
- `alert_log.py` — appends fired alerts (with odds attached) to `alerts_log.json` (capped at 500), read by `dashboard.html`.
- `paths.py` — shared data-file location. Defaults to the project folder; set `DATA_DIR` (e.g. a Railway Volume mount) so state/alerts/odds survive redeploys.
- `app.py` — Flask entrypoint for deployment: runs the polling loop in a background thread, serves `dashboard.html` plus `status.json` / `alerts_log.json` / `odds_snapshot.json`.
- `dashboard.html` — static HTML/JS dashboard (live game odds grid + alert history), same visual pattern as `Claude-Test/`. Auto-refreshes every 15s.

**Deployment:** Deployed to Railway from `https://github.com/nickwells21/MLB-Blowout-Bot`, connected via GitHub auto-deploy. Env vars set in Railway match `.env.example`; `DATA_DIR` points at a mounted Volume for persistence.

**Not included (by design):** never places bets automatically — it only alerts you to place the bet yourself.

**Known limitation:** no backtested win rate yet — this implements the detection/alerting/odds-display mechanism only. Validate the actual edge (how often the spread really does move favorably) before betting real money on every alert.
