"""Live MLB odds via The Odds API (the-odds-api.com).

Two-tier fetching to keep quota under control:
- Main markets (spreads, h2h, totals) come from the bulk `/odds` endpoint in a
  single call that returns every MLB game today. Cost: 3 credits per call.
- Alternate run-line markets can ONLY be fetched from the per-event endpoint
  `/events/{event_id}/odds`. We call it once per live game, cached with its own
  TTL so it refreshes less often than the main fetch. Cost: 1 credit per game
  per call.

At the paid tier (20K credits/month), defaults give roughly:
  main:   3 credits * 12/hour * 10h * 30d ~= 10,800 credits
  alts:  10 credits *  4/hour * 10h * 30d ~= 12,000 credits  (10 games avg)
Total ~22K vs 20K budget — trim MAIN_REFRESH_SECONDS or ALT_REFRESH_SECONDS
via env if you actually hit the cap during a busy stretch.
"""
import os
import time

import requests

API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
MAIN_REFRESH_SECONDS = int(os.environ.get("ODDS_REFRESH_SECONDS", "300"))
ALT_REFRESH_SECONDS = int(os.environ.get("ODDS_ALT_REFRESH_SECONDS", "900"))
BOOK = os.environ.get("ODDS_BOOK", "draftkings").strip()

BASE = "https://api.the-odds-api.com/v4/sports/baseball_mlb"
MAIN_MARKETS = ["spreads", "h2h", "totals"]

_main_cache = None
_main_cache_at = 0.0
_alt_cache = {}
_quota_used = None
_quota_remaining = None


def _record_quota(resp):
    global _quota_used, _quota_remaining
    _quota_used = resp.headers.get("x-requests-used", _quota_used)
    _quota_remaining = resp.headers.get("x-requests-remaining", _quota_remaining)


def _fetch_main():
    if not API_KEY:
        return None
    resp = requests.get(
        f"{BASE}/odds",
        params={
            "apiKey": API_KEY,
            "regions": "us",
            "markets": ",".join(MAIN_MARKETS),
            "oddsFormat": "american",
        },
        timeout=15,
    )
    resp.raise_for_status()
    _record_quota(resp)
    return resp.json()


def _fetch_event_alternates(event_id):
    if not API_KEY or not event_id:
        return None
    resp = requests.get(
        f"{BASE}/events/{event_id}/odds",
        params={
            "apiKey": API_KEY,
            "regions": "us",
            "markets": "alternate_spreads",
            "oddsFormat": "american",
        },
        timeout=15,
    )
    resp.raise_for_status()
    _record_quota(resp)
    return resp.json()


def get_cached_or_fetch():
    """Return the raw list of MAIN-market game odds, using in-memory cache."""
    global _main_cache, _main_cache_at
    if not API_KEY:
        return None
    if _main_cache is not None and (time.time() - _main_cache_at) < MAIN_REFRESH_SECONDS:
        return _main_cache
    try:
        data = _fetch_main()
    except requests.RequestException as e:
        print(f"[odds] Main fetch failed: {e}")
        return _main_cache
    if data is not None:
        _main_cache = data
        _main_cache_at = time.time()
    return _main_cache


def get_alternates_for_event(event_id):
    """Return the alternate-spreads game object for a single event, cached
    per-event with ALT_REFRESH_SECONDS TTL."""
    if not API_KEY or not event_id:
        return None
    entry = _alt_cache.get(event_id)
    if entry and (time.time() - entry["at"]) < ALT_REFRESH_SECONDS:
        return entry["data"]
    try:
        data = _fetch_event_alternates(event_id)
    except requests.RequestException as e:
        print(f"[odds] Alt fetch failed for {event_id}: {e}")
        return entry["data"] if entry else None
    _alt_cache[event_id] = {"data": data, "at": time.time()}
    return data


def quota_status():
    return {
        "used": _quota_used,
        "remaining": _quota_remaining,
        "main_cached_at": _main_cache_at if _main_cache_at else None,
        "main_refresh_seconds": MAIN_REFRESH_SECONDS,
        "alt_refresh_seconds": ALT_REFRESH_SECONDS,
    }


def extract_for_book(main_game, alt_game, book_key):
    """Merge main-market and (optional) alternate-spread data for one game/
    book into a flat dict used by the dashboard."""
    if not main_game:
        return None
    home = main_game.get("home_team")
    away = main_game.get("away_team")
    result = {
        "home_team": home,
        "away_team": away,
        "book": book_key,
        "spreads_home": [],
        "spreads_away": [],
    }
    seen_home = set()
    seen_away = set()

    def ingest_bookmakers(game, source_label):
        for bm in game.get("bookmakers", []):
            if bm.get("key") != book_key:
                continue
            result["book_title"] = bm.get("title", book_key)
            for market in bm.get("markets", []):
                mkey = market.get("key")
                outcomes = market.get("outcomes", [])
                if mkey in ("spreads", "alternate_spreads"):
                    for o in outcomes:
                        side = "home" if o.get("name") == home else "away"
                        point = o.get("point")
                        price = o.get("price")
                        if point is None:
                            continue
                        seen = seen_home if side == "home" else seen_away
                        if point in seen:
                            continue
                        seen.add(point)
                        result[f"spreads_{side}"].append({"point": point, "price": price})
                elif mkey == "h2h":
                    for o in outcomes:
                        side = "home" if o.get("name") == home else "away"
                        result[f"moneyline_{side}"] = o.get("price")
                elif mkey == "totals":
                    for o in outcomes:
                        if o.get("name") == "Over":
                            result["total_point"] = o.get("point")
                            result["total_over_price"] = o.get("price")
                        elif o.get("name") == "Under":
                            result["total_under_price"] = o.get("price")

    ingest_bookmakers(main_game, "main")
    if alt_game:
        ingest_bookmakers(alt_game, "alt")

    if not result.get("book_title"):
        return None

    # Home ladder ascending (most negative first — biggest favorite cover on top)
    # Away ladder descending (biggest underdog cover on top)
    result["spreads_home"].sort(key=lambda x: x["point"])
    result["spreads_away"].sort(key=lambda x: -x["point"])
    return result


def find_game(all_odds, home_team, away_team):
    for g in all_odds or []:
        if g.get("home_team") == home_team and g.get("away_team") == away_team:
            return g
    return None


def fetch_for_alert(home_team, away_team):
    """Fresh (uncached) main + alt fetch for a single matchup at alert time."""
    if not API_KEY:
        return None
    try:
        main_data = _fetch_main()
    except requests.RequestException as e:
        print(f"[odds] Alert-time main fetch failed: {e}")
        return None
    main_game = find_game(main_data, home_team, away_team)
    if not main_game:
        return None
    alt_game = None
    try:
        alt_game = _fetch_event_alternates(main_game.get("id"))
    except requests.RequestException as e:
        print(f"[odds] Alert-time alt fetch failed: {e}")
    extracted = extract_for_book(main_game, alt_game, BOOK)
    if extracted:
        home_first = extracted["spreads_home"][0] if extracted.get("spreads_home") else {}
        away_first = extracted["spreads_away"][0] if extracted.get("spreads_away") else {}
        extracted["spread_home"] = home_first.get("point")
        extracted["spread_home_price"] = home_first.get("price")
        extracted["spread_away"] = away_first.get("point")
        extracted["spread_away_price"] = away_first.get("price")
        extracted["ml_home"] = extracted.get("moneyline_home")
        extracted["ml_away"] = extracted.get("moneyline_away")
    return extracted
