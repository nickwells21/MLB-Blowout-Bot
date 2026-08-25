# MLB Blowout Bot

**Purpose:** Watches live MLB games and alerts (push notification) when a losing team brings in a position player to pitch during a blowout. Betting angle: bet the winning team's spread to increase from that point, since the position player appearance signals the losing team has conceded.

**Setup:**
```bash
cd "C:/Users/nickw/Claudes Folder/MLB-Blowout-Bot"
pip install -r requirements.txt
cp .env.example .env
# Set NTFY_TOPIC in .env to something unique (e.g. nick-mlb-blowout-7x2q),
# then install the ntfy app (iOS/Android) and subscribe to that same topic.
```

**Run:**
```bash
python bot.py
```
Leave it running during game hours (or wrap it in a scheduled task) — it polls live games every `POLL_INTERVAL_SECONDS` (default 30) and pushes an alert the moment a position player takes the mound in a game that's a blowout by `BLOWOUT_RUN_DIFF` runs (default 6).

**Dashboard:**
```bash
python -m http.server 8090
# Open http://localhost:8090/dashboard.html
```
Static page reading `status.json` (bot heartbeat) and `alerts_log.json` (alert history) via fetch — auto-refreshes every 15s. Must be served over HTTP (not opened as a `file://` path) for fetch to work.

**Architecture:**
- `mlb_api.py` — wraps the free MLB Stats API (statsapi.mlb.com, no key required): live game lookup, boxscore fetch, and the core detection logic (a player who appears in a team's `pitchers` list but whose real position isn't "P"/"TWP" is a position player pitching).
- `bot.py` — polling loop, blowout-threshold check, alert composition, writes `status.json` heartbeat each cycle.
- `notifier.py` — pushes to ntfy.sh (free, no signup). Swap in Pushover/Twilio here if you want SMS instead.
- `state.py` — persists which events have already been alerted on (`state.json`) so restarts/re-polls don't spam duplicates.
- `alert_log.py` — appends fired alerts to `alerts_log.json` (capped at 500), read by `dashboard.html`.
- `dashboard.html` — static HTML/JS dashboard, same pattern as `Claude-Test/`.

**Not included (by design):** no odds API integration yet, and it never places bets automatically — it only alerts you to place the bet yourself. Add live spread tracking once you've picked an odds provider (e.g. The Odds API).

**Known limitation:** no backtested win rate yet — this implements the detection/alerting mechanism only. Validate the actual edge (how often the spread really does move favorably) before betting real money on every alert.
