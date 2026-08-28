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
Either way it polls live games every `POLL_INTERVAL_SECONDS` (default 30) and pushes an alert the moment a position player takes the mound. (`BLOWOUT_RUN_DIFF` no longer exists — the run-diff bar is `RUN_DIFF_MID`/`RUN_DIFF_LATE`, and it grades the alert rather than gating it.) `app.py` runs this same loop in a background thread and serves the dashboard over HTTP — this is the version deployed to Railway (see `Procfile`).

**Rule hierarchy — read this before touching the tiers.**

There is one signal that matters and six that support it.

*TIER 0 — GOLDEN.* A position player is pitching. This is the entire premise
of the bot. It **overrides every other rule and always fires** — `_classify`
never returns None; the tiers below it (`bullpen_exhausted`, `blowout`,
`position_player`) only grade how strong the betting case is. Two rules protect
it structurally: it runs **first** in `check_game`, before anything that can
fail, and needs nothing but the boxscore. Everything it touches afterwards
(odds, alert log) is wrapped in `_safe()` so a dependency failure degrades the
message instead of losing the push. Never move this below the context tiers,
and never add a gate that can return no alert.

**There is no minimum inning, deliberately.** The bet is the winning side's
spread *widening* from the moment of concession, so an early concession leaves
more innings for that to happen, not fewer. A 3rd-inning position player is if
anything a better spot than an 8th-inning one. `RUN_DIFF_MID`/`RUN_DIFF_LATE`
still scale the required differential *down* after the 7th — that is a
relaxation late, never a floor early. Do not reintroduce an inning gate.

*TIER 1 — CONTEXT.* Bullpen Depth, Big Lead Watch, Extreme Lead, Urgency Mode,
Inning Report, Pitcher Change. These **do not gate Tier 0**. They exist to flag games where a
position player is becoming likely, so you are already watching when it
happens. Each runs through `_safe()` so one failing tier cannot suppress the
others — a rate-limited ntfy push used to abort the whole game's checks,
including the golden one.

**Send order is data, not call order.** A phone stack shows the newest push on
top, so whatever is sent *last* is what you see. That used to be decided by the
order `check_game()` happened to call the tiers, which put the low-value inning
ping on top of the bullpen-exhaustion ping it should sit under (user-reported),
and — worse — put GOLDEN, which correctly runs first, at the very bottom.

Tiers no longer push. They write into an `AlertBus`, which flushes the poll's
batch sorted by `ALERT_RANK` in **ascending importance**, so the most important
alert is sent last and lands on top:

```
urgency_off 10 < inning_digest 20 < big_lead 30 < extreme_lead 40
  < urgency_on 50 < pitcher_change 60 < bullpen_exhausted 70
  < bullpen_critical 80 < golden 100
```

Computation order and send order are now separate concerns, which is what lets
GOLDEN be **computed first** (nothing that can fail may precede it) *and* **sent
last** (nothing may cover it) — two requirements that are contradictory as long
as a tier pushes the instant it decides to. The flush sits in a `finally`, and
each send is individually isolated, so neither a tier escaping `_safe()` nor a
rate-limited push can drop the golden alert queued behind it.

Adding a tier means adding a rank. Forgetting is safe by construction: an
unregistered kind sorts below everything (`UNRANKED_ALERT_RANK`) and logs a
warning, so a new tier can never bury a critical one wherever its call lands.

**Routine per-inning traffic is consolidated.** `check_inning_report` sends ONE
push per half-inning carrying the inning, the current lead (and its delta since
the previous report), and the trailing team's bullpen state. Big Lead Watch
folds into it via `merge_into` when both land on the same poll — and into the
Urgency Mode ON message, which already prints the same header. Extreme Lead
deliberately does **not** fold: a lead running away is escalation, not routine,
and it outranks the report. Mid-inning, Big Lead and Extreme Lead still push
immediately and separately, so a lead that jumps between innings is never held
back to the next boundary. A fold whose target isn't in the batch emits on its
own, so consolidation can never silently drop an alert.

The report is suppressed when Urgency Mode entry or a bullpen rung fired on the
same poll — both already carry score, inning and bullpen state. `_last_half_inning`
is advanced by `consume_inning_change()` at the top of every poll, before any
tier can raise, so a suppressed or folded boundary can never re-fire later.

The hourly slate summary is held back a poll when any game alert fired in the
same cycle — it is sent after the per-game batches and would otherwise sit on
top of them. The clock is not advanced, so it goes out on the next quiet poll
rather than being skipped.

**The bullpen depth ladder** (`check_bullpen_depth`) is the escalation that
leads into Tier 0. It fires on the trailing team's reliever count alone — no
run-diff bar, no inning floor — once per rung per (game, side):

| Relievers used | Alert | Priority |
| --- | --- | --- |
| `BULLPEN_EXHAUSTION_COUNT` (3) | Bullpen Exhausted | high |
| `BULLPEN_CRITICAL_COUNT` (4) | Bullpen Exhausted — URGENT | urgent |
| position player on the mound | **GOLDEN** (Tier 0) | urgent |

The ladder stops at the critical rung; ongoing churn past it belongs to Pitcher
Change. Two guards keep one substitution from pushing twice: if the arm that
trips a rung *is* a position player the ladder stays silent and lets Tier 0 own
the moment, and a rung that fires passes `suppress=True` into both
`check_pitcher_change` and `check_inning_report`, exactly as urgency-mode entry
does. Both guards still mark the rung as alerted, so a suppressed rung can never
fire late.

Keys are `bullpen:{game_pk}:{side}:{rung}` in the persisted `alerted` set, so a
restart mid-game does not re-announce a rung, and a lead change correctly starts
a fresh ladder for the newly trailing team.

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
- `mlb_api.py` — wraps the free MLB Stats API (statsapi.mlb.com, no key required): live game lookup, boxscore fetch, and the core detection logic (a player who appears in a team's `pitchers` list but whose real position isn't "P"/"TWP" is a position player pitching). `get_bullpen_detail()` returns each arm's full outing line — IP, pitches, strikes/balls and a computed strike %, H, R, ER, BB, K, HR, batters faced, outs — plus season ERA, MLB's decision string, and `is_starter` / `is_current` / `is_position_player` flags. **All of it is pulled out of the boxscore response the bot already fetches on every poll, so the fuller line costs zero extra API requests and cannot affect polling frequency.** Never add a separate fetch to enrich a pitcher row; if a stat isn't in the boxscore, it doesn't go on the dashboard.
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
- `dashboard.html` — static HTML/JS dashboard (live game odds grid + bullpens + alert history), same visual pattern as `Claude-Test/`. Auto-refreshes every 15s. The Bullpens tab (desktop `tabBullpen`, mobile `gBullpen`) lists each team's arms chronologically with the full per-pitcher stat line, and both views share the `statTone()` / `sv()` helpers so the colouring is identical. **The colour convention is deliberate: red is damage the pitcher allowed, green is damage he avoided** — so reading down the trailing team's column, a wall of red is the bullpen coming apart, which is the state a position player emerges from. A position-player row is tinted red and tagged with its real position, outranking the amber "now" marker on the current pitcher. Don't invert the scale or retune the thresholds without a reason; the scan-down read is the point.

**Deployment:** Deployed to Railway from `https://github.com/nickwells21/MLB-Blowout-Bot`, connected via GitHub auto-deploy. Env vars set in Railway match `.env.example`; `DATA_DIR` points at a mounted Volume for persistence.

**Not included (by design):** never places bets automatically — it only alerts you to place the bet yourself.

**Known limitation:** no backtested win rate yet — this implements the detection/alerting/odds-display mechanism only. Validate the actual edge (how often the spread really does move favorably) before betting real money on every alert.
