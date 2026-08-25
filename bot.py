"""
MLB Blowout Bot

Watches live MLB games and alerts when a losing team brings in a position
player to pitch during a blowout — historically a signal that the team has
conceded the game, which tends to correlate with the winning team's final
margin growing further. The betting angle: the winning team's spread is
likely to increase (they cover a bigger number) from this point on.

This bot only detects and alerts. It never places bets for you.

Usage:
    python bot.py

Config (env vars, see .env.example):
    NTFY_TOPIC              ntfy.sh topic to push alerts to (required for push;
                             falls back to console-only if unset)
    BLOWOUT_RUN_DIFF         minimum run differential to consider it a blowout (default 6)
    POLL_INTERVAL_SECONDS    how often to poll live games (default 30)
"""
import os
import time

from dotenv import load_dotenv

load_dotenv()

import alert_log
import mlb_api
import notifier
import odds
import paths
import state

BLOWOUT_RUN_DIFF = int(os.environ.get("BLOWOUT_RUN_DIFF", "6"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))


def _now_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def check_game(game_pk, alerted):
    boxscore = mlb_api.get_boxscore(game_pk)
    hits = mlb_api.find_position_players_pitching(boxscore)
    if not hits:
        return

    linescore = mlb_api.get_linescore(boxscore)

    for hit in hits:
        key = f"{game_pk}:{hit['player_id']}"
        if key in alerted:
            continue

        conceding_side = hit["side"]
        bet_side = "home" if conceding_side == "away" else "away"

        conceding_runs = linescore[conceding_side]
        bet_runs = linescore[bet_side]
        run_diff = bet_runs - conceding_runs

        # Only alert if the position player's team is actually losing by enough.
        if run_diff < BLOWOUT_RUN_DIFF:
            continue

        bet_team = linescore[f"{bet_side}_name"]
        conceding_team = linescore[f"{conceding_side}_name"]

        title = f"Position player pitching: {conceding_team} down {run_diff}"
        message = (
            f"{hit['name']} ({hit['real_position']}) is pitching for {conceding_team}.\n"
            f"Score: {bet_team} {bet_runs} - {conceding_team} {conceding_runs}\n"
            f"Signal: {conceding_team} has conceded. Consider betting {bet_team}'s "
            f"spread to increase (cover a bigger number) from here."
        )

        home_team = linescore["home_name"]
        away_team = linescore["away_name"]
        odds_snapshot = odds.fetch_for_alert(home_team, away_team)

        if odds_snapshot:
            bet_spread = odds_snapshot.get(f"spread_{bet_side}")
            bet_spread_price = odds_snapshot.get(f"spread_{bet_side}_price")
            if bet_spread is not None:
                message += (
                    f"\nCurrent {odds_snapshot.get('book_title', 'book')} line: "
                    f"{bet_team} {bet_spread:+g} ({bet_spread_price:+d})"
                )

        print(f"\n=== ALERT ===\n{title}\n{message}\n")
        notifier.send_alert(title, message)
        alert_log.append(
            {
                "timestamp": _now_iso(),
                "game_pk": game_pk,
                "player_name": hit["name"],
                "player_position": hit["real_position"],
                "conceding_team": conceding_team,
                "bet_team": bet_team,
                "bet_team_runs": bet_runs,
                "conceding_team_runs": conceding_runs,
                "run_diff": run_diff,
                "odds_at_alert": odds_snapshot,
            }
        )
        alerted.add(key)


def _write_status(live_game_count):
    import json

    with open(paths.data_path("status.json"), "w") as f:
        json.dump(
            {
                "last_checked": _now_iso(),
                "live_games": live_game_count,
                "blowout_run_diff": BLOWOUT_RUN_DIFF,
                "poll_interval_seconds": POLL_INTERVAL_SECONDS,
            },
            f,
            indent=2,
        )


def _write_live_snapshot(game_pks):
    """For each live game, fetch score/inning/count and match with odds
    (moneyline, totals, run-line ladder). Writes live_snapshot.json for the
    dashboard. Skips the odds API entirely when no games are live to protect
    the free-tier quota."""
    import json

    payload = {
        "fetched_at": time.time(),
        "book": odds.BOOK,
        "quota": odds.quota_status() if game_pks else None,
        "games": [],
    }

    all_odds = odds.get_cached_or_fetch() if game_pks else None
    # quota_status() reflects the fetch that may have just happened above
    if game_pks:
        payload["quota"] = odds.quota_status()

    for pk in game_pks:
        try:
            box = mlb_api.get_boxscore(pk)
            detail = mlb_api.get_linescore_detail(pk)
            home_team = box["teams"]["home"]["team"]["name"]
            away_team = box["teams"]["away"]["team"]["name"]
            game_odds = None
            if all_odds:
                match = odds.find_game(all_odds, home_team, away_team)
                if match:
                    alt = odds.get_alternates_for_event(match.get("id"))
                    game_odds = odds.extract_for_book(match, alt, odds.BOOK)
            payload["games"].append(
                {
                    "game_pk": pk,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_runs": detail.get("home_runs", 0),
                    "away_runs": detail.get("away_runs", 0),
                    "inning": detail.get("inning"),
                    "inning_ordinal": detail.get("inning_ordinal"),
                    "inning_state": detail.get("inning_state"),
                    "balls": detail.get("balls"),
                    "strikes": detail.get("strikes"),
                    "outs": detail.get("outs"),
                    "odds": game_odds,
                }
            )
        except Exception as e:
            print(f"[bot] Snapshot error for game {pk}: {e}")

    with open(paths.data_path("live_snapshot.json"), "w") as f:
        json.dump(payload, f, indent=2)


def run_once(alerted):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # MLB game dates are in ET. Using the server's local date breaks when the
    # process runs in UTC (e.g. Railway) between midnight ET and 3am ET —
    # date.today() rolls over to tomorrow while west-coast games are still live.
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    game_pks = mlb_api.get_live_game_pks(today)
    for game_pk in game_pks:
        try:
            check_game(game_pk, alerted)
        except Exception as e:
            print(f"[bot] Error checking game {game_pk}: {e}")
    _write_status(len(game_pks))
    _write_live_snapshot(game_pks)


def main():
    print(f"MLB Blowout Bot starting. Blowout threshold: {BLOWOUT_RUN_DIFF} runs, "
          f"polling every {POLL_INTERVAL_SECONDS}s.")
    alerted = state.load_alerted()
    try:
        while True:
            run_once(alerted)
            state.save_alerted(alerted)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n[bot] Stopped.")
        state.save_alerted(alerted)


if __name__ == "__main__":
    main()
