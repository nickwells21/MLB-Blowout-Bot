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
