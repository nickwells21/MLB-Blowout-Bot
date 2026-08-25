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

**Architecture:**
- `mlb_api.py` — wraps the free MLB Stats API (statsapi.mlb.com, no key required): live game lookup, boxscore fetch, and the core detection logic (a player who appears in a team's `pitchers` list but whose real position isn't "P"/"TWP" is a position player pitching).
- `odds.py` — wraps The Odds API for live spread/moneyline/total data. `maybe_refresh_snapshot()` writes a dashboard-wide snapshot (`odds_snapshot.json`) on a slow cadence (`ODDS_REFRESH_SECONDS`); `fetch_for_alert()` does one fresh fetch at alert time so the recorded line matches the moment the position player enters. No-ops entirely if `ODDS_API_KEY` is unset.
- `bot.py` — polling loop, blowout-threshold check, alert composition (attaches the current line from `odds.py` when available), writes `status.json` heartbeat each cycle.
- `notifier.py` — pushes to ntfy.sh (free, no signup). Swap in Pushover/Twilio here if you want SMS instead.
- `state.py` — persists which events have already been alerted on (`state.json`) so restarts/re-polls don't spam duplicates.
- `alert_log.py` — appends fired alerts (with odds attached) to `alerts_log.json` (capped at 500), read by `dashboard.html`.
- `paths.py` — shared data-file location. Defaults to the project folder; set `DATA_DIR` (e.g. a Railway Volume mount) so state/alerts/odds survive redeploys.
- `app.py` — Flask entrypoint for deployment: runs the polling loop in a background thread, serves `dashboard.html` plus `status.json` / `alerts_log.json` / `odds_snapshot.json`.
- `dashboard.html` — static HTML/JS dashboard (live game odds grid + alert history), same visual pattern as `Claude-Test/`. Auto-refreshes every 15s.

**Deployment:** Deployed to Railway from `https://github.com/nickwells21/MLB-Blowout-Bot`, connected via GitHub auto-deploy. Env vars set in Railway match `.env.example`; `DATA_DIR` points at a mounted Volume for persistence.

**Not included (by design):** never places bets automatically — it only alerts you to place the bet yourself.

**Known limitation:** no backtested win rate yet — this implements the detection/alerting/odds-display mechanism only. Validate the actual edge (how often the spread really does move favorably) before betting real money on every alert.
