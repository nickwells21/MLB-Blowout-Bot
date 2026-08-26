"""MLB schedule awareness, so the bot can sleep between slates instead of
polling the live-games endpoint 24/7.

TIMEZONE RULE (this project has been burned by this before): MLB game dates are
EASTERN, not server-local and not UTC. A west-coast night game that starts
2026-08-27T01:05:00Z has `officialDate` 2026-08-26 -- it belongs to the Aug 26
slate, and a bot that sleeps at UTC midnight kills itself mid-game. So:

  * "which day is it" always comes from `today_et()` (ZoneInfo America/New_York),
    never `date.today()`.
  * "which day does a game belong to" always comes from the API's `officialDate`
    field, never from slicing the UTC `gameDate`.

READING compute_window() -- distinguishing "no games" from "games not started":

    game_count == 0          -> genuine off day / All-Star break. `all_final` is
                                True and `first_pitch_utc` is None because there
                                is nothing to wait for. Sleep until tomorrow.
    game_count > 0
      and all_final is False
      and first_pitch_utc
          in the future      -> slate exists but hasn't started. Sleep until
                                first_pitch_utc.
    game_count > 0
      and all_final is False
      and first_pitch_utc
          in the past        -> slate in progress. Poll.
    all_final is True
      with game_count > 0    -> slate is done. Sleep until tomorrow.

Never treat `first_pitch_utc is None` on its own as "no games" -- check
`game_count`. Both cases give None.

BACKSTOP: a suspended or stuck game can sit non-Final indefinitely, so
`all_final` alone is not a safe "we're done" signal. `backstop_utc`
(last scheduled start + BACKSTOP_HOURS) is the hard give-up time: once the
clock passes it, shut down for the night regardless of `all_final`.

POSTPONED/CANCELLED games are excluded from `game_count`, never set
`first_pitch_utc`, and never block `all_final`. Note the trap: MLB reports these
with `abstractGameState` "Final" and only `detailedState` "Postponed"/
"Cancelled", so a naive count treats them as real games that already ended --
inflating `game_count` and making a 1-game day look like a 2-game day. Both the
numerator and the denominator here skip them, so `games_final == game_count`
reflects only games actually played. SUSPENDED games are deliberately NOT
treated this way (a suspended game may still resume); the backstop covers those.

When a postponed game is rescheduled, MLB leaves the row in the ORIGINAL date's
block but rewrites its `officialDate` to the makeup date -- so filtering on
officialDate drops it from the day it was called off and picks it up, played and
Final, on the day it was actually made up. Same for a suspended game resumed the
next day: it appears in both days' blocks but keeps the original `officialDate`,
so it is counted once, on the slate it belongs to, with its original start time.

ERRORS: `get_day_games` / `compute_window` re-raise `requests.RequestException`
when the API is unreachable AND nothing is cached for that date. They do not
quietly return an empty slate, because "no games" reads as "sleep" and would
silently skip a whole day. Callers should wrap these and default to staying
awake on error. A failed refresh with a stale cache entry present serves the
stale entry instead of raising (stale data errs toward staying awake).
"""
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import paths

BASE = "https://statsapi.mlb.com/api/v1"

ET = ZoneInfo("America/New_York")

# Hard "give up and sleep" margin after the last scheduled first pitch. A
# suspended game can sit non-Final forever, so the caller needs a wall-clock
# bound that does not depend on any game reaching Final.
BACKSTOP_HOURS = int(os.environ.get("SCHEDULE_BACKSTOP_HOURS", "7"))

# Per-day in-memory TTL so a 30s poll loop doesn't hammer the schedule endpoint.
DAY_CACHE_SECONDS = int(os.environ.get("SCHEDULE_CACHE_SECONDS", "300"))

# End bound for fetch_season(): past the last possible World Series game.
SEASON_END_DATE = os.environ.get("SCHEDULE_SEASON_END", "2026-11-30")

SEASON_FILE = paths.data_path("season_schedule.json")

# detailedState substrings meaning "this game was not played". These carry
# abstractGameState "Final", so they must be filtered on detailedState or they
# masquerade as completed games. Matched case-insensitively and as substrings
# because MLB writes variants like "Postponed" and "Cancelled" (and occasionally
# the American "Canceled"). "Suspended" and "Delayed" are deliberately absent --
# those games can still resume.
DEAD_STATE_MARKERS = ("postponed", "cancel")

_day_cache = {}


def today_et():
    """'YYYY-MM-DD' for the current Eastern-time date -- the MLB slate date."""
    return datetime.now(ET).date().isoformat()


def _parse_utc(value):
    """Parse the API's `gameDate` ('2026-08-26T17:10:00Z') into an aware UTC
    datetime. Python < 3.11 cannot read the trailing 'Z', so swap it for
    '+00:00' first. Returns None on anything unparseable."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_dead(detailed_state):
    text = (detailed_state or "").lower()
    return any(marker in text for marker in DEAD_STATE_MARKERS)


def _sort_key(game):
    """Sort by start time ascending, pushing unparseable starts to the end so
    they can never masquerade as first pitch."""
    start = game.get("start_utc")
    return (
        start is None,
        start or datetime.max.replace(tzinfo=timezone.utc),
        game.get("game_pk") or 0,
    )


def _record(side):
    """'78-54' from a schedule entry's leagueRecord, or None."""
    rec = (side or {}).get("leagueRecord") or {}
    w, l = rec.get("wins"), rec.get("losses")
    return f"{w}-{l}" if w is not None and l is not None else None


def _normalize(raw):
    status = raw.get("status") or {}
    teams = raw.get("teams") or {}
    away_side = teams.get("away") or {}
    home_side = teams.get("home") or {}
    away = away_side.get("team") or {}
    home = home_side.get("team") or {}
    detailed_state = status.get("detailedState")
    return {
        "game_pk": raw.get("gamePk"),
        "start_utc": _parse_utc(raw.get("gameDate")),
        # officialDate is the ET slate date and is the ONLY correct answer to
        # "which day is this game on". Fall back to the UTC date prefix only if
        # the field is missing outright.
        "official_date": raw.get("officialDate") or (raw.get("gameDate") or "")[:10],
        "status": status.get("abstractGameState"),
        "detailed_state": detailed_state,
        "away": away.get("name"),
        "home": home.get("name"),
        # Team ids drive the logo URLs on the dashboard
        # (https://www.mlbstatic.com/team-logos/{id}.svg).
        "away_id": away.get("id"),
        "home_id": home.get("id"),
        "away_record": _record(away_side),
        "home_record": _record(home_side),
        # Present on live/final games only; None before first pitch.
        "away_score": away_side.get("score"),
        "home_score": home_side.get("score"),
        "game_type": raw.get("gameType"),
        "is_postponed": _is_dead(detailed_state),
    }


def _fetch(params):
    resp = requests.get(f"{BASE}/schedule", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _games_from_payload(payload, date_str=None):
    """Flatten every `dates[].games[]` entry, optionally keeping only those
    whose officialDate matches date_str. Doubleheaders survive this because
    they share an officialDate but carry different gamePks."""
    games = []
    for block in (payload or {}).get("dates", []):
        for raw in block.get("games", []):
            game = _normalize(raw)
            if date_str is not None and game["official_date"] != date_str:
                continue
            games.append(game)
    games.sort(key=_sort_key)
    return games


def get_day_games(date_str, force_refresh=False):
    """All games whose officialDate == date_str, sorted by start_utc ascending.

    Each dict: {game_pk, start_utc (aware UTC datetime), official_date, status,
    detailed_state, away, home, game_type, is_postponed}.

    Cached in memory for DAY_CACHE_SECONDS. Raises requests.RequestException if
    the API is unreachable and nothing is cached for this date.
    """
    entry = _day_cache.get(date_str)
    if entry and not force_refresh and (time.time() - entry["at"]) < DAY_CACHE_SECONDS:
        return list(entry["games"])
    try:
        payload = _fetch({"sportId": 1, "date": date_str})
    except requests.RequestException as e:
        if entry:
            age = int(time.time() - entry["at"])
            print(f"[schedule] Fetch failed for {date_str} ({e}); serving cache {age}s old.")
            return list(entry["games"])
        raise
    games = _games_from_payload(payload, date_str)
    _day_cache[date_str] = {"games": games, "at": time.time()}
    return list(games)


def _summarize(games):
    """Window summary for an already-fetched list of normalized games."""
    playable = [g for g in games if not g["is_postponed"]]
    starts = [g["start_utc"] for g in playable if g["start_utc"] is not None]
    first_pitch = min(starts) if starts else None
    last_start = max(starts) if starts else None
    games_final = sum(1 for g in playable if g["status"] == "Final")
    return {
        "game_count": len(playable),
        "first_pitch_utc": first_pitch,
        "last_scheduled_start_utc": last_start,
        "backstop_utc": (last_start + timedelta(hours=BACKSTOP_HOURS)) if last_start else None,
        # No games at all -> nothing to wait for -> already "done".
        "all_final": games_final == len(playable),
        "games_final": games_final,
    }


def compute_window(date_str):
    """Polling window for one ET slate date. See the module docstring for how to
    tell "no games" apart from "games have not started yet"."""
    summary = _summarize(get_day_games(date_str))
    return {"date": date_str, **summary}


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _jsonable(window):
    return {k: _iso(v) for k, v in window.items()}


def _date_range(start_date, end_date):
    current = date.fromisoformat(start_date)
    stop = date.fromisoformat(end_date)
    while current <= stop:
        yield current.isoformat()
        current += timedelta(days=1)


def _day_entry(date_str, games):
    return {
        "date": date_str,
        **_jsonable(_summarize(games)),
        "games": [_jsonable(g) for g in games],
    }


def fetch_season(start_date=None, end_date=None):
    """Pull the whole remaining season in ONE ranged request and cache it to
    paths.data_path('season_schedule.json').

    Defaults: today_et() through SEASON_END_DATE (2026-11-30, past the last
    possible World Series game). Returns
    {'fetched_at', 'start_date', 'end_date', 'days': {'YYYY-MM-DD': {...window
    summary..., 'games': [...]}}}.

    Every date in the range gets an entry, including off days (game_count 0), so
    the caller can look up any date without a second request. Datetimes are
    written as ISO-8601 UTC strings ('2026-08-26T17:10:00+00:00'), readable back
    with datetime.fromisoformat.
    """
    start_date = start_date or today_et()
    end_date = end_date or SEASON_END_DATE
    payload = _fetch({"sportId": 1, "startDate": start_date, "endDate": end_date})

    by_date = {}
    for game in _games_from_payload(payload):
        by_date.setdefault(game["official_date"], []).append(game)

    days = {}
    # Seed the full range so off days are explicit "0 games" entries rather than
    # missing keys the caller has to guess about.
    for date_str in _date_range(start_date, end_date):
        games = sorted(by_date.pop(date_str, []), key=_sort_key)
        days[date_str] = _day_entry(date_str, games)
    # A resumed suspended game can carry an officialDate outside the requested
    # range. Keep those rather than dropping them on the floor.
    for date_str in sorted(by_date):
        games = sorted(by_date[date_str], key=_sort_key)
        days[date_str] = _day_entry(date_str, games)

    season = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
    }
    with open(SEASON_FILE, "w") as f:
        json.dump(season, f, indent=2)
    return season


def load_season():
    """Read back season_schedule.json, or None if absent/unreadable. Datetime
    fields come back as ISO-8601 strings, not datetime objects."""
    if not os.path.exists(SEASON_FILE):
        return None
    try:
        with open(SEASON_FILE, "r") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"[schedule] Could not read {SEASON_FILE}: {e}")
        return None


if __name__ == "__main__":
    day = today_et()
    window = compute_window(day)
    print(f"[schedule] Today (ET): {day}")
    for key, value in window.items():
        print(f"  {key}: {value}")
    for game in get_day_games(day):
        flag = " POSTPONED" if game["is_postponed"] else ""
        print(
            f"  {game['start_utc']}  {game['game_pk']}  {game['away']} @ {game['home']}"
            f"  [{game['status']}/{game['detailed_state']}]{flag}"
        )
