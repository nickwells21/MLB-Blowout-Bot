"""Thin wrapper around the free MLB Stats API (statsapi.mlb.com). No API key required."""
import json
import os

import requests

import paths

BASE = "https://statsapi.mlb.com/api/v1"

# --- Roster position cache -------------------------------------------------
# A boxscore's `position` field is what a player is playing RIGHT NOW, not who
# they are. The instant a position player takes the mound MLB relabels them
# "P", which makes the boxscore useless for the one question this bot exists to
# answer. `primaryPosition` from /people is the roster position and is the only
# field that stays truthful.
#
# Primary positions do not change mid-season, so they are cached hard: in
# memory, and on disk so a restart does not refetch hundreds of players.
POSITIONS_FILE = paths.data_path("player_positions.json")
_primary_pos = None
_positions_refreshed_at = None  # ISO string, None if never swept


def _load_positions():
    global _primary_pos, _positions_refreshed_at
    if _primary_pos is not None:
        return _primary_pos
    _primary_pos = {}
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict) and "players" in data:
                _positions_refreshed_at = data.get("refreshed_at")
                _primary_pos = {int(k): v for k, v in data["players"].items()}
            else:
                # Pre-sweep flat format ({id: {...}}): usable, but counts as
                # never having done a full league sweep.
                _primary_pos = {int(k): v for k, v in data.items()}
        except (OSError, ValueError, TypeError) as e:
            print(f"[mlb_api] Could not read {POSITIONS_FILE}: {e}")
            _primary_pos = {}
    return _primary_pos


def _save_positions():
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(
                {"refreshed_at": _positions_refreshed_at, "players": _primary_pos},
                f,
                indent=2,
            )
    except OSError as e:
        print(f"[mlb_api] Could not write {POSITIONS_FILE}: {e}")


def positions_refreshed_at():
    """ISO timestamp of the last full league sweep, or None."""
    _load_positions()
    return _positions_refreshed_at


def refresh_league_positions(season=None):
    """Full league roster sweep: ONE request to /sports/1/players returns every
    MLB player for the season with their primaryPosition. This is the record
    the detector runs against -- the boxscore's own position field relabels a
    position player as "P" the moment they take the mound, so it must never be
    used as the label.

    Merges into the permanent cache (never clears it: a player who appeared
    earlier but has since been outrighted should keep resolving). Returns the
    number of players now on record, or None on failure -- callers treat a
    failed sweep as non-fatal because the lazy per-game lookup still works."""
    global _positions_refreshed_at
    from datetime import datetime, timezone

    if season is None:
        # The season is the ET year -- a January sweep of "next year" 404s
        # gracefully into an empty list, which we treat as failure, not truth.
        from zoneinfo import ZoneInfo

        season = datetime.now(ZoneInfo("America/New_York")).year
    cache = _load_positions()
    try:
        resp = requests.get(
            f"{BASE}/sports/1/players", params={"season": season}, timeout=30
        )
        resp.raise_for_status()
        people = resp.json().get("people", [])
    except requests.RequestException as e:
        print(f"[mlb_api] League roster sweep failed (non-fatal): {e}")
        return None
    if not people:
        print(f"[mlb_api] League roster sweep returned no players for {season}; keeping existing record.")
        return None
    for person in people:
        pos = person.get("primaryPosition") or {}
        if pos.get("abbreviation"):
            cache[person["id"]] = {
                "abbreviation": pos.get("abbreviation"),
                "name": pos.get("name"),
            }
    _positions_refreshed_at = datetime.now(timezone.utc).isoformat()
    _save_positions()
    print(f"[mlb_api] League roster sweep: {len(people)} players, {len(cache)} on record.")
    return len(cache)


def get_primary_positions(person_ids):
    """{person_id: {'abbreviation','name'}} for each id, from the roster rather
    than the live boxscore. Batched (the /people endpoint takes many ids at
    once) and cached permanently. Ids that cannot be resolved are simply absent
    from the result -- callers must handle that rather than assuming a hit."""
    cache = _load_positions()
    missing = sorted({int(i) for i in person_ids if i is not None} - set(cache))
    if missing:
        fetched = 0
        for i in range(0, len(missing), 40):
            chunk = missing[i:i + 40]
            try:
                resp = requests.get(
                    f"{BASE}/people",
                    params={"personIds": ",".join(str(x) for x in chunk)},
                    timeout=20,
                )
                resp.raise_for_status()
                for person in resp.json().get("people", []):
                    pos = person.get("primaryPosition") or {}
                    if pos.get("abbreviation"):
                        cache[person["id"]] = {
                            "abbreviation": pos.get("abbreviation"),
                            "name": pos.get("name"),
                        }
                        fetched += 1
            except requests.RequestException as e:
                # Transient failure: leave these uncached so the next poll
                # retries. Never write a guess into the cache.
                print(f"[mlb_api] Primary-position lookup failed for {len(chunk)} id(s): {e}")
        if fetched:
            _save_positions()
    return {i: cache[i] for i in (int(x) for x in person_ids if x is not None) if i in cache}


def is_position_player(person_id, fallback_abbr=None):
    """True when this player's ROSTER position is not pitcher. Falls back to the
    boxscore abbreviation only when the roster lookup is unavailable, which
    preserves the old (blind) behaviour rather than inventing an alert."""
    pos = get_primary_positions([person_id]).get(int(person_id)) if person_id is not None else None
    abbr = (pos or {}).get("abbreviation") or fallback_abbr
    return abbr is not None and abbr not in ("P", "TWP")


def get_live_game_pks(date_str):
    """Return gamePks for games currently in progress on the given YYYY-MM-DD date."""
    resp = requests.get(
        f"{BASE}/schedule",
        params={"sportId": 1, "date": date_str},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    pks = []
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            if game["status"]["abstractGameState"] == "Live":
                pks.append(game["gamePk"])
    return pks


def get_boxscore(game_pk):
    resp = requests.get(f"{BASE}/game/{game_pk}/boxscore", timeout=15)
    resp.raise_for_status()
    return resp.json()


def find_position_players_pitching(boxscore):
    """
    Scan a boxscore for players who have appeared as pitcher this game but whose
    ROSTER position is not pitcher (and not a legitimate two-way player).

    The check must run against primaryPosition from /people, not the boxscore's
    own position field: the boxscore reports what a player is doing right now,
    so the moment a position player takes the mound it relabels them "P" --
    which hid every single real event from the old implementation (verified
    across Aug 24-26: Johnston RF, Acuna SS, Pages C, Vivas 3B, all shown as
    "P" in their boxscores).

    Returns a list of dicts: {side, player_id, name, real_position}
    """
    # One batched lookup for every pitcher in the game (cached after first hit).
    all_ids = []
    for side in ("away", "home"):
        team = boxscore["teams"][side]
        players = team.get("players", {})
        for pid in team.get("pitchers", []):
            player = players.get(f"ID{pid}")
            if player:
                all_ids.append(player["person"]["id"])
    roster_pos = get_primary_positions(all_ids)

    hits = []
    for side in ("away", "home"):
        team = boxscore["teams"][side]
        players = team.get("players", {})
        for pid in team.get("pitchers", []):
            player = players.get(f"ID{pid}")
            if not player:
                continue
            person_id = player["person"]["id"]
            primary = roster_pos.get(person_id)
            if primary is not None:
                abbr = primary.get("abbreviation")
                pos_name = primary.get("name", "Unknown")
            else:
                # Roster lookup unavailable (API hiccup): fall back to the
                # boxscore field. Misses stay possible in this state, but no
                # false alerts are invented.
                box_position = player.get("position", {})
                abbr = box_position.get("abbreviation")
                pos_name = box_position.get("name", "Unknown")
            if abbr not in ("P", "TWP"):
                hits.append(
                    {
                        "side": side,
                        "player_id": pid,
                        "name": player["person"]["fullName"],
                        "real_position": pos_name,
                    }
                )
    return hits


def get_linescore(boxscore):
    """Pull current runs for each side out of a boxscore's team stats."""
    result = {}
    for side in ("away", "home"):
        team = boxscore["teams"][side]
        result[side] = team.get("teamStats", {}).get("batting", {}).get("runs", 0)
        result[f"{side}_name"] = team["team"]["name"]
    return result


def get_linescore_detail(game_pk):
    """Fetch the live linescore endpoint for a game and return the current
    inning/half, ball-strike-out count, and score. Used to render the live
    dashboard row for each in-progress game."""
    resp = requests.get(f"{BASE}/game/{game_pk}/linescore", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    teams = data.get("teams") or {}
    return {
        "inning": data.get("currentInning"),
        "inning_ordinal": data.get("currentInningOrdinal"),
        "inning_state": data.get("inningState"),
        "balls": data.get("balls"),
        "strikes": data.get("strikes"),
        "outs": data.get("outs"),
        "home_runs": (teams.get("home") or {}).get("runs", 0),
        "away_runs": (teams.get("away") or {}).get("runs", 0),
    }


def count_pitchers_used(boxscore, side):
    """Number of distinct pitchers who have appeared for this team so far
    this game (includes whoever is currently pitching -- e.g. the position
    player). Subtract one from this to get prior relievers burned before the
    position player entered; used as the bullpen-exhaustion signal."""
    team = boxscore["teams"][side]
    return len(team.get("pitchers", []))


def get_bullpen_detail(boxscore, side):
    """Ordered list of every pitcher a team has used this game, chronological
    (starter first, current pitcher last). Each entry: name, innings_pitched
    (MLB's own "3.1" notation), pitches (numberOfPitches this outing),
    is_current (True only for the last entry — MLB writes final pitch counts
    into the boxscore as soon as a pitcher exits, so past entries' counts
    lock in automatically without any bookkeeping on our side)."""
    team = boxscore["teams"][side]
    pitcher_ids = team.get("pitchers", [])
    players = team.get("players", {})
    person_ids = [
        players[f"ID{pid}"]["person"]["id"]
        for pid in pitcher_ids
        if players.get(f"ID{pid}")
    ]
    roster_pos = get_primary_positions(person_ids)
    detail = []
    for idx, pid in enumerate(pitcher_ids):
        player = players.get(f"ID{pid}")
        if not player:
            continue
        pitching = player.get("stats", {}).get("pitching", {}) or {}
        season = player.get("seasonStats", {}).get("pitching", {}) or {}
        primary = roster_pos.get(player["person"]["id"]) or {}
        box_abbr = player.get("position", {}).get("abbreviation")
        real_abbr = primary.get("abbreviation") or box_abbr
        strikes = pitching.get("strikes")
        pitches = pitching.get("numberOfPitches")
        detail.append(
            {
                "player_id": pid,
                "name": player.get("person", {}).get("fullName", "Unknown"),
                # Roster position -- shows "3B" for a position player pitching,
                # where the boxscore field would claim "P".
                "position": real_abbr,
                "is_position_player": real_abbr not in ("P", "TWP") if real_abbr else False,
                "innings_pitched": pitching.get("inningsPitched"),
                "pitches": pitches,
                # --- this outing's line. Every field below already rides along
                # in the boxscore we fetch each poll, so the fuller line costs
                # no extra request and cannot affect polling frequency. ---
                "strikes": strikes,
                "balls": pitching.get("balls"),
                "strike_pct": round(100 * strikes / pitches) if pitches else None,
                "hits": pitching.get("hits"),
                "runs": pitching.get("runs"),
                "earned_runs": pitching.get("earnedRuns"),
                "walks": pitching.get("baseOnBalls"),
                "strikeouts": pitching.get("strikeOuts"),
                "home_runs": pitching.get("homeRuns"),
                "batters_faced": pitching.get("battersFaced"),
                "outs": pitching.get("outs"),
                # Season ERA for context on whether this arm is a soft spot.
                "era": season.get("era"),
                # MLB's own decision string, e.g. "(W, 9-2)" / "(H, 12)".
                "note": pitching.get("note"),
                "is_starter": idx == 0,
                "is_current": idx == len(pitcher_ids) - 1,
            }
        )
    return detail


def get_starter_info(boxscore, side):
    """Return {'player_id', 'name', 'innings_pitched'} for the side's starter
    (the first entry in the team's chronological `pitchers` list), or None
    if unavailable. innings_pitched is MLB's own notation (e.g. '3.1' = 3 1/3
    innings), taken straight from the boxscore so it needs no conversion."""
    team = boxscore["teams"][side]
    pitcher_ids = team.get("pitchers", [])
    if not pitcher_ids:
        return None
    starter_id = pitcher_ids[0]
    player = team.get("players", {}).get(f"ID{starter_id}")
    if not player:
        return None
    ip = player.get("stats", {}).get("pitching", {}).get("inningsPitched")
    return {
        "player_id": starter_id,
        "name": player.get("person", {}).get("fullName"),
        "innings_pitched": ip,
    }
