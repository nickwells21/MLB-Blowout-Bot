"""Thin wrapper around the free MLB Stats API (statsapi.mlb.com). No API key required."""
import requests

BASE = "https://statsapi.mlb.com/api/v1"


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
    real position is not pitcher (and not a legitimate two-way player).

    Returns a list of dicts: {side, player_id, name, real_position}
    """
    hits = []
    for side in ("away", "home"):
        team = boxscore["teams"][side]
        players = team.get("players", {})
        for pid in team.get("pitchers", []):
            player = players.get(f"ID{pid}")
            if not player:
                continue
            position = player.get("position", {})
            abbr = position.get("abbreviation")
            if abbr not in ("P", "TWP"):
                hits.append(
                    {
                        "side": side,
                        "player_id": pid,
                        "name": player["person"]["fullName"],
                        "real_position": position.get("name", "Unknown"),
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
