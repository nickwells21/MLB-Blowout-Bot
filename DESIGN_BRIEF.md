# MLB Blowout Bot — UI Design Brief

Paste this whole file into Claude Design. It is self-contained: it explains the product, gives the exact live data contract with real sample payloads, and states what the screens need to do.

The backend is **finished and deployed**. Do not design around data that isn't listed here — everything below is real and live right now.

---

## 1. What this product is

A betting-signal dashboard for MLB blowouts. It watches every live game and pushes phone alerts. **It never places bets. It only signals.**

**The thesis, in one sentence:** when a team is losing badly enough that they send a *position player* (a fielder — catcher, infielder, outfielder) to the mound instead of a real pitcher, they have publicly conceded the game, and you bet the winning team's alternate run-line spread to widen further.

That single event is called the **golden signal**. Everything else on the dashboard exists to help the user see it coming.

The supporting signals, in rough order of importance:

| Signal | Meaning |
|---|---|
| **Position player pitching** | The bet. Fires instantly, always, at urgent priority. |
| **Bullpen exhaustion** | Trailing team has burned 3+ relievers (4+ is critical). The state a position player emerges from. |
| **Urgency mode** | Lead 6+ *and* trailing team past 2+ relievers. A latched state, not a moment. |
| **Extreme lead** | Lead of 10+, then every 2 runs beyond. |
| **Big lead** | Lead of 6+, then every 3 runs beyond. |
| **Inning / pitcher change** | Routine texture. Lowest value. |

**Design consequence:** the golden signal must be impossible to miss and impossible to confuse with anything else. A user glancing at a phone for two seconds should be able to tell "a fielder is pitching" apart from "the lead went up." These are not peers and should not look like peers.

---

## 2. The user

One person, a personal trainer who builds his own tools. He watches this on an **iPhone** while games are on, and on desktop when at his computer. Mobile is not an afterthought — it is the primary surface during a live slate.

He reads it under time pressure. A bet on a widening spread is only good for as long as the line lasts.

---

## 3. Data contract

Base URL: `https://web-production-586f7.up.railway.app`

Four endpoints. **All four always return valid JSON** — they never 404 and never return an error page, even before the bot has written anything.

| Endpoint | Shape | Notes |
|---|---|---|
| `/live_snapshot.json` | `{ "games": [...] }` | The main feed. Every game on today's slate. |
| `/status.json` | object | Bot health + every rule threshold. |
| `/alerts_log.json` | array | History. **Golden signals only** — see §3.4. |
| `/odds_snapshot.json` | `{ "games": [...] }` | Raw odds. The per-game `odds` object in the main feed is usually enough. |

**Refresh:** poll every 15 seconds. The bot itself polls MLB every 30s normally and speeds to 15s when any game is hot, so 15s on the UI side never lags the data.

### 3.1 A real game object

This is an actual live payload, captured from production (bullpens and odds ladders trimmed for length — they are longer in reality):

```json
{
  "game_pk": 822771,
  "status": "Live",
  "detailed_state": "In Progress",
  "is_live": true,
  "start_utc": "2026-08-27T23:07:00+00:00",
  "away_team": "Kansas City Royals",
  "home_team": "Toronto Blue Jays",
  "away_id": 118,
  "home_id": 141,
  "away_record": "59-75",
  "home_record": "65-69",
  "away_runs": 10,
  "home_runs": 2,
  "inning": 7,
  "inning_ordinal": "7th",
  "inning_state": "Bottom",
  "balls": 0,
  "strikes": 0,
  "outs": 2,
  "away_hits": 10,
  "home_hits": 7,
  "away_errors": 0,
  "home_errors": 1,
  "away_lob": 7,
  "home_lob": 5,
  "is_top_inning": false,
  "scheduled_innings": 9,
  "batting_side": "home",
  "fielding_side": "away",
  "batter": "Vladimir Guerrero Jr.",
  "on_deck": "Alejandro Kirk",
  "current_pitcher": "Alex Lange",
  "defense": {
    "catcher": "Carter Jensen",
    "first": "Vinnie Pasquantino",
    "second": "Isaac Collins",
    "third": "Josh Rojas",
    "shortstop": "Bobby Witt Jr.",
    "left": "John Rave",
    "center": "Kyle Isbel",
    "right": "Jac Caglianone"
  },
  "innings": [
    { "num": 1, "ordinal": "1st", "away_runs": 0, "home_runs": 0 },
    { "num": 2, "ordinal": "2nd", "away_runs": 3, "home_runs": 0 },
    { "num": 3, "ordinal": "3rd", "away_runs": 0, "home_runs": 1 }
  ],
  "away_bullpen": [ /* see 3.2 */ ],
  "home_bullpen": [ /* see 3.2 */ ],
  "odds": { /* see 3.3 */ },
  "urgency": true
}
```

**Every key above is always present**, on scheduled and finished games too. Before first pitch the live-only fields are `null`, `[]`, or `{}` — never missing. You never need to test for a missing key.

Field notes that matter for layout:

- `status` is `"Preview"` | `"Live"` | `"Final"`. `is_live` is the boolean you should actually branch on.
- `inning_state` is `"Top"` | `"Bottom"` | `"Middle"` | `"End"`. During `Middle`/`End` nobody is batting — `batter` will still hold the last value, so prefer showing the half-inning state over a stale batter.
- `batting_side` / `fielding_side` are `"away"` | `"home"`, precomputed so you don't have to reason about `is_top_inning`.
- `defense` names the fielders **of the team currently in the field**. For this product that is a meaningful list, not trivia: these are the position players who could be sent to pitch. If the fielding side is also the side getting blown out, this is the shortlist of who the golden signal will name.
- `innings` is the classic linescore grid. The current inning's entry can have `null` runs if the half hasn't been played yet.
- `urgency` is a latched boolean for the whole game.
- `away_record` / `home_record` are strings like `"59-75"`.

### 3.2 A pitcher object (inside `away_bullpen` / `home_bullpen`)

Chronological: **starter first, current pitcher last.** This ordering is the point — it reads as a story of the game falling apart.

```json
{
  "player_id": 681293,
  "name": "Spencer Arrighetti",
  "position": "P",
  "is_position_player": false,
  "innings_pitched": "4.0",
  "pitches": 79,
  "strikes": 49,
  "balls": 30,
  "strike_pct": 62,
  "hits": 5,
  "runs": 4,
  "earned_runs": 3,
  "walks": 2,
  "strikeouts": 4,
  "home_runs": 1,
  "batters_faced": 20,
  "outs": 12,
  "era": "4.81",
  "note": null,
  "is_starter": true,
  "is_current": false
}
```

- **`is_position_player: true` is the golden signal.** When true, `position` holds their real roster position (`"C"`, `"1B"`, `"LF"`…). This row must be the loudest thing on the screen.
- `position` is the player's **roster** position, deliberately not their in-game one. MLB relabels a position player as `"P"` the moment they pitch, so the in-game label would make the signal invisible. This field is corrected for that.
- `innings_pitched` is a **string** in baseball notation: `"1.1"` means 1⅓ innings, not 1.1. Never do math on it; `outs` is the numeric equivalent.
- `era` is a **string** (season ERA, for context on whether this arm is a soft spot). `note` is MLB's decision string like `"(W, 9-2)"` and is usually `null`.
- `strike_pct` is an integer percentage, or `null` when the pitcher has been announced but hasn't thrown a pitch. **This happens in production** — render an em dash, never `NaN`.
- `is_current: true` on the pitcher on the mound right now.

### 3.3 The odds object (`odds`, may be `null`)

```json
{
  "book": "draftkings",
  "book_title": "DraftKings",
  "away_team": "Kansas City Royals",
  "home_team": "Toronto Blue Jays",
  "spreads_away": [ { "point": -8.5, "price": 300 }, { "point": -9.0, "price": 475 } ],
  "spreads_home": [ { "point": 8.5, "price": -440 }, { "point": 9.0, "price": -775 } ],
  "total_point": 14.5,
  "total_over_price": -101,
  "total_under_price": -129
}
```

`odds` is `null` whenever the book hasn't posted the game — **design the empty state, it is common.** The spread arrays are the *alternate run-line ladder*, roughly 15–17 rungs each, and are the actual betting instrument this whole product exists to serve. Prices are American odds (show `+` explicitly on positives).

### 3.4 `status.json` — bot health and every threshold

```json
{
  "last_checked": "2026-08-28T01:22:23.070108+00:00",
  "live_games": 4,
  "bot_state": "scanning",
  "schedule": {
    "date": "2026-08-27", "games_today": 7, "games_final": 2,
    "first_pitch": "2026-08-27T17:05:00+00:00",
    "last_scheduled_start": "2026-08-28T01:45:00+00:00",
    "next_wake": null
  },
  "run_diff_mid": 6, "run_diff_late": 4,
  "bullpen_exhaustion_count": 3, "bullpen_critical_count": 4,
  "big_lead_threshold": 6, "big_lead_step": 3,
  "extreme_lead_threshold": 10, "extreme_lead_step": 2,
  "urgency_run_diff": 6, "urgency_min_relievers": 2, "urgency_exit_run_diff": 4,
  "poll_interval_seconds": 15, "base_poll_interval_seconds": 30,
  "fast_poll_interval_seconds": 15, "fast_poll_active": true
}
```

`bot_state` is `"scanning"` (a slate is running) or `"sleeping"` (bot is off until the next first pitch — normal and correct, not an error; do not design it as a failure state). `fast_poll_active` means at least one game is hot.

**Thresholds are served, not hardcoded.** Read them from here so the UI can never disagree with the bot.

`/alerts_log.json` contains **only golden signals** — every entry is a real position-player event with the odds captured at alert time. It is legitimately an empty array `[]` much of the time; these events are rare, which is exactly why they're worth betting. Design the empty state as "nothing yet," not as an error.

---

## 4. Screens to design

### A. Slate — all of today's games
The landing view. Every game as a card, sorted so the most interesting is first (biggest lead / urgency / bullpen depth). Cards must work in three states: **scheduled** (no score yet), **live**, **final**.

Show team logos. Logo URL is `https://www.mlbstatic.com/team-logos/{team_id}.svg` using `away_id` / `home_id`. Cards should be clickable into the detail view.

### B. Game detail
The working surface. Needs to cover:

1. **Live state** — score, inning + half, count, outs, R/H/E, the inning-by-inning grid, who's batting, who's pitching.
2. **Pitcher columns (the most important panel)** — both teams' pitchers, chronological, each with their full line: IP, pitches, strike %, H, R, ER, BB, K, season ERA. See §5 for the required color treatment.
3. **The alternate run-line ladder** — the actual bet. Both sides.
4. **Bot read** — why this game is or isn't signaling, stated in plain language against the served thresholds.
5. **The fielders** (`defense`) — optional but thematically strong: the pool of position players who might take the mound.

### C. Alert history
Past golden signals with the odds captured at the moment each fired.

### D. Rules reference
A plain-language statement of the signal hierarchy in §1. The user specifically values being able to audit what the bot will and won't do.

---

## 5. Required visual conventions

These two are load-bearing. Everything else is open.

**1. Red means damage allowed, green means damage avoided.** Applied to every pitcher stat: hits, runs, earned runs, walks, home runs go red as they climb; strikeouts and a healthy strike rate go green; a high pitch count goes amber then red. The reason this matters: reading down the losing team's pitcher column, **a wall of red *is* the bullpen coming apart** — the user should diagnose a collapsing pen from color alone, before reading a single number. Numbers must align in true columns (tabular figures) so the eye can scan straight down.

**2. A position player on the mound outranks every other treatment.** When `is_position_player` is true, that row wins over any "current pitcher" styling and carries their real position (`C`, `1B`) as a badge. It should be visible from across the room.

## 6. Aesthetic direction

Established preferences, worth keeping:

- **Black base.** Deep near-black ground, not grey.
- **Soft ambient team color.** A gentle glow/halo around team logos in their team's color, with the overall page staying dark and calm. Ambient, not loud.
- **Games outlined in a thin grey line with rounded corners.**
- Dense and information-first. A previous layout was rejected specifically for **hiding the pitcher ladders and bullpens behind a click** — density lost to prettiness. Do not bury the pitcher data.
- Numeric/tabular data in a monospaced or tabular-figure face so columns align.

Reference points the user likes: `thedatastreak.com/mlb/dashboard` (logo-forward game boxes, upcoming games visible). A carousel-style tile layout was tried and **rejected** — don't go that direction.

---

## 7. Things that will bite you

- `innings_pitched` is a string in baseball notation (`"1.1"` = 1⅓). Not a decimal.
- `era` is a string. `strike_pct` can be `null`. `note` is usually `null`.
- `odds` is `null` often. Design that empty state properly.
- `alerts_log.json` is frequently `[]`. That's success, not failure.
- `bot_state: "sleeping"` is normal between slates.
- Bullpen arrays are `[]` for games that haven't started.
- Both teams have a bullpen list. The interesting one is the **trailing** team's — derive from the scores.
- Times are **UTC ISO strings**. The user is in **US Central**; convert for display.

---

## 8. What does not exist

Do not design UI for these — there is no data behind them:

- **Line movement over time.** Odds are point-in-time only; there is no historical series. A sparkline of "how the spread moved since the alert" cannot be populated.
- Win probability, projected final score, or any model output. This bot does not predict; it detects.
- Player photos or headshots.
- Betting slips, stake sizing, or bankroll tracking. The bot signals; the user places bets himself, elsewhere.
