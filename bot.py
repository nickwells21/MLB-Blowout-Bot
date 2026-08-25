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
    MIN_INNING               floor inning for the ordinary blowout tier (default 5)
    RUN_DIFF_MID              run diff needed in innings MIN_INNING-6 (default = BLOWOUT_RUN_DIFF)
    RUN_DIFF_LATE             run diff needed in innings 7+ (default 4)
    BULLPEN_EXHAUSTION_COUNT  prior relievers used that triggers the higher-priority
                              "bullpen exhausted" tier, bypassing inning/run-diff (default 4)
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

# --- Rule engine v2 (inning-aware + bullpen-exhaustion tier) ---
# A flat run-diff knob doesn't distinguish a 6-run lead in the 3rd (nothing)
# from a 6-run lead in the 8th (real). MIN_INNING is a hard floor: the
# ordinary "blowout" tier never fires before this inning. RUN_DIFF_MID/LATE
# scale the required run differential down as the game gets later.
MIN_INNING = int(os.environ.get("MIN_INNING", "5"))
RUN_DIFF_MID = int(os.environ.get("RUN_DIFF_MID", str(BLOWOUT_RUN_DIFF)))  # innings MIN_INNING-6
RUN_DIFF_LATE = int(os.environ.get("RUN_DIFF_LATE", "4"))                  # innings 7+

# Bullpen exhaustion is a primary signal in its own right: if the losing
# team already burned this many relievers before turning to a position
# player, that's alert-worthy regardless of inning or run differential --
# this tier bypasses MIN_INNING/RUN_DIFF entirely and fires as high priority.
BULLPEN_EXHAUSTION_COUNT = int(os.environ.get("BULLPEN_EXHAUSTION_COUNT", "4"))


def _now_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def required_run_diff_for_inning(inning):
    """Run differential needed to trigger the ordinary 'blowout' tier at a
    given inning. Falls back to BLOWOUT_RUN_DIFF if inning is unknown."""
    if inning is None:
        return BLOWOUT_RUN_DIFF
    if inning <= 6:
        return RUN_DIFF_MID
    return RUN_DIFF_LATE


def _classify(hit, boxscore, linescore, inning):
    """Decide whether a position-player-pitching event clears an alert tier.

    Returns (tier, run_diff, prior_relievers). tier is None if nothing fired.
    "bullpen_exhausted" bypasses the inning/run-diff gate entirely -- burning
    through the bullpen before resorting to a position player is treated as
    the strongest form of the concession signal. "blowout" is the ordinary,
    inning-scaled tier.
    """
    conceding_side = hit["side"]
    bet_side = "home" if conceding_side == "away" else "away"

    conceding_runs = linescore[conceding_side]
    bet_runs = linescore[bet_side]
    run_diff = bet_runs - conceding_runs

    # Pitchers list includes whoever is pitching right now (the position
    # player) -- subtract them to get relievers actually burned beforehand.
    prior_relievers = mlb_api.count_pitchers_used(boxscore, conceding_side) - 1

    if run_diff <= 0:
        # Not actually losing right now -- no concession signal either way.
        return None, run_diff, prior_relievers

    if prior_relievers >= BULLPEN_EXHAUSTION_COUNT:
        return "bullpen_exhausted", run_diff, prior_relievers

    if inning is not None and inning >= MIN_INNING and run_diff >= required_run_diff_for_inning(inning):
        return "blowout", run_diff, prior_relievers

    return None, run_diff, prior_relievers


def check_game(game_pk, alerted):
    boxscore = mlb_api.get_boxscore(game_pk)
    hits = mlb_api.find_position_players_pitching(boxscore)
    if not hits:
        return

    linescore = mlb_api.get_linescore(boxscore)
    detail = mlb_api.get_linescore_detail(game_pk)
    inning = detail.get("inning")
    inning_ordinal = detail.get("inning_ordinal")
    inning_state = detail.get("inning_state")
    outs = detail.get("outs")

    for hit in hits:
        key = f"{game_pk}:{hit['player_id']}"
        if key in alerted:
            continue

        tier, run_diff, prior_relievers = _classify(hit, boxscore, linescore, inning)
        if tier is None:
            continue

        conceding_side = hit["side"]
        bet_side = "home" if conceding_side == "away" else "away"
        bet_team = linescore[f"{bet_side}_name"]
        conceding_team = linescore[f"{conceding_side}_name"]
        bet_runs = linescore[bet_side]
        conceding_runs = linescore[conceding_side]

        if tier == "bullpen_exhausted":
            title = "Bullpen Exhausted Alert"
            priority = "urgent"
            tags = "rotating_light,fire"
        else:
            title = "Blowout Alert"
            priority = "high"
            tags = "baseball,rotating_light"

        if inning_state and inning_ordinal:
            inning_line = f"{inning_state} {inning_ordinal}"
        elif inning is not None:
            inning_line = f"Inning {inning}"
        else:
            inning_line = "Inning unknown"
        if outs is not None:
            inning_line += f", {outs} out{'s' if outs != 1 else ''}"

        description = (
            f"{hit['name']} ({hit['real_position']}) is pitching for {conceding_team}. "
            f"{conceding_team} has conceded -- consider betting {bet_team}'s spread to "
            f"increase (cover a bigger number) from here."
        )
        if tier == "bullpen_exhausted":
            description += f" {prior_relievers} reliever(s) already used before this move."
        elif prior_relievers:
            description += f" {prior_relievers} reliever(s) already used this game."

        # Starter context: only meaningful if someone pitched before the
        # position player (otherwise the "starter" and the position player
        # are the same person, which isn't a useful line to print).
        if prior_relievers >= 1:
            starter_info = mlb_api.get_starter_info(boxscore, conceding_side)
            if starter_info and starter_info.get("innings_pitched") is not None:
                description += (
                    f" Starter {starter_info['name']} went {starter_info['innings_pitched']} IP."
                )

        message = (
            f"{bet_team} {bet_runs} - {conceding_team} {conceding_runs}\n"
            f"{inning_line}\n\n"
            f"{description}"
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

            bet_ml = odds_snapshot.get(f"ml_{bet_side}")
            conceding_ml = odds_snapshot.get(f"ml_{conceding_side}")
            if bet_ml is not None:
                ml_line = f"\nMoneyline: {bet_team} {bet_ml:+d}"
                if conceding_ml is not None:
                    ml_line += f" / {conceding_team} {conceding_ml:+d}"
                message += ml_line

            total_point = odds_snapshot.get("total_point")
            if total_point is not None:
                total_over = odds_snapshot.get("total_over_price")
                total_under = odds_snapshot.get("total_under_price")
                total_line = f"\nTotal: {total_point:g}"
                if total_over is not None and total_under is not None:
                    total_line += f" (O {total_over:+d} / U {total_under:+d})"
                message += total_line

        print(f"\n=== ALERT [{tier}] ===\n{title}\n{message}\n")
        notifier.send_alert(title, message, priority=priority, tags=tags)
        alert_log.append(
            {
                "timestamp": _now_iso(),
                "game_pk": game_pk,
                "tier": tier,
                "inning": inning,
                "prior_relievers_used": prior_relievers,
                "player_name": hit["name"],
                "player_position": hit["real_position"],
                "conceding_team": conceding_team,
                "bet_team": bet_team,
                "bet_team_runs": bet_runs,
                "conceding_team_runs": conceding_runs,
                "run_diff": run_diff,
                "outs": outs,
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
                "min_inning": MIN_INNING,
                "run_diff_mid": RUN_DIFF_MID,
                "run_diff_late": RUN_DIFF_LATE,
                "bullpen_exhaustion_count": BULLPEN_EXHAUSTION_COUNT,
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
    print(
        f"MLB Blowout Bot starting. Blowout tier: inning>={MIN_INNING}, "
        f"{RUN_DIFF_MID}+ runs (innings {MIN_INNING}-6) / {RUN_DIFF_LATE}+ runs (7+). "
        f"Bullpen-exhausted tier: {BULLPEN_EXHAUSTION_COUNT}+ prior relievers (any inning). "
        f"Polling every {POLL_INTERVAL_SECONDS}s."
    )
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
