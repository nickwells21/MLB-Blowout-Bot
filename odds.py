"""Live MLB odds via The Odds API (the-odds-api.com).

Free tier is 500 credits/month; 1 credit = 1 market * 1 region per call. We
fetch spreads + moneyline + totals from US books on a slow cadence (default
15 min via ODDS_REFRESH_SECONDS) and write a pre-extracted snapshot to
odds_snapshot.json for the dashboard. At alert time we also do one fresh
fetch so the recorded odds match the moment the position player enters.
"""
import json
import os
import time

import requests

import paths

API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
REFRESH_SECONDS = int(os.environ.get("ODDS_REFRESH_SECONDS", "900"))
BOOK = os.environ.get("ODDS_BOOK", "draftkings").strip()
SNAPSHOT_FILE = paths.data_path("odds_snapshot.json")

API_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
MARKETS = ["spreads", "h2h", "totals"]


def _fetch():
    if not API_KEY:
        return None
    params = {
        "apiKey": API_KEY,
        "regions": "us",
        "markets": ",".join(MARKETS),
        "oddsFormat": "american",
    }
    resp = requests.get(API_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _extract(game, book_key):
    result = {
        "home_team": game.get("home_team"),
        "away_team": game.get("away_team"),
        "commence_time": game.get("commence_time"),
        "book": book_key,
    }
    for bm in game.get("bookmakers", []):
        if bm.get("key") != book_key:
            continue
        result["book_title"] = bm.get("title", book_key)
        for market in bm.get("markets", []):
            mkey = market.get("key")
            outcomes = market.get("outcomes", [])
            if mkey == "spreads":
                for o in outcomes:
                    side = "home" if o.get("name") == game.get("home_team") else "away"
                    result[f"spread_{side}"] = o.get("point")
                    result[f"spread_{side}_price"] = o.get("price")
            elif mkey == "h2h":
                for o in outcomes:
                    side = "home" if o.get("name") == game.get("home_team") else "away"
                    result[f"ml_{side}"] = o.get("price")
            elif mkey == "totals":
                for o in outcomes:
                    if o.get("name") == "Over":
                        result["total"] = o.get("point")
                        result["total_over_price"] = o.get("price")
                    elif o.get("name") == "Under":
                        result["total_under_price"] = o.get("price")
        break
    return result


def _snapshot_age_seconds():
    if not os.path.exists(SNAPSHOT_FILE):
        return None
    return time.time() - os.path.getmtime(SNAPSHOT_FILE)


def maybe_refresh_snapshot():
    """Fetch fresh odds and rewrite odds_snapshot.json if it's older than
    REFRESH_SECONDS. Safe to call every bot poll cycle."""
    if not API_KEY:
        return
    age = _snapshot_age_seconds()
    if age is not None and age < REFRESH_SECONDS:
        return
    try:
        data = _fetch()
    except requests.RequestException as e:
        print(f"[odds] Snapshot refresh failed: {e}")
        return
    if data is None:
        return
    games = [_extract(g, BOOK) for g in data]
    payload = {
        "fetched_at": time.time(),
        "book": BOOK,
        "refresh_seconds": REFRESH_SECONDS,
        "games": games,
    }
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(payload, f, indent=2)


def fetch_for_alert(home_team, away_team):
    """Fresh (uncached) fetch for a single matchup at alert time. Returns the
    extracted per-book dict or None if the game or book isn't available."""
    if not API_KEY:
        return None
    try:
        data = _fetch()
    except requests.RequestException as e:
        print(f"[odds] Alert-time fetch failed: {e}")
        return None
    for game in data or []:
        if game.get("home_team") == home_team and game.get("away_team") == away_team:
            return _extract(game, BOOK)
    return None
