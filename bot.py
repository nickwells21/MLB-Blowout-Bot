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

try:
    import schedule
except ImportError:  # scheduling is optional -- fall back to 24/7 polling
    schedule = None

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

# How often (seconds) to push a slate-wide summary, independent of any
# individual alert. Rolling cadence from process start, not wall-clock-
# aligned (i.e. "every hour on the hour" would need different logic).
SUMMARY_INTERVAL_SECONDS = int(os.environ.get("SUMMARY_INTERVAL_SECONDS", "3600"))
_last_summary_at = time.time()

# Big-lead watch tier: independent of the position-player detection entirely.
# Fires on run differential alone, any inning, re-alerting every BIG_LEAD_STEP
# additional runs so growing blowouts keep surfacing. Also arms the
# pitcher-change tier below for that game.
BIG_LEAD_THRESHOLD = int(os.environ.get("BIG_LEAD_THRESHOLD", "6"))
BIG_LEAD_STEP = int(os.environ.get("BIG_LEAD_STEP", "3"))

# Extreme-lead watch: once a lead is truly out-of-hand (10+ runs by default),
# re-alert on tighter increments (every 2 runs: 10, 12, 14, ...) at urgent
# priority and specifically ask you to watch for bullpen churn. Big Lead
# suppresses inside extreme territory so the two tiers don't double-ping at
# overlapping buckets (e.g. lead=12 hits both otherwise).
EXTREME_LEAD_THRESHOLD = int(os.environ.get("EXTREME_LEAD_THRESHOLD", "10"))
EXTREME_LEAD_STEP = int(os.environ.get("EXTREME_LEAD_STEP", "2"))

# --- Urgency mode ---
# A per-game latched state, not a one-shot alert. A game enters urgency mode
# when the lead is URGENCY_RUN_DIFF+ AND the trailing team has already burned
# URGENCY_MIN_RELIEVERS+ relievers -- i.e. the concession is plausibly already
# underway. While a game is in this mode, EVERY half-inning change and EVERY
# pitcher change for the trailing team pushes a notification, so the position
# player move can't slip by unseen.
#
# Exit uses hysteresis (URGENCY_EXIT_RUN_DIFF < URGENCY_RUN_DIFF) so a game
# hovering around the entry threshold doesn't flap in and out of the mode.
URGENCY_RUN_DIFF = int(os.environ.get("URGENCY_RUN_DIFF", "6"))
URGENCY_MIN_RELIEVERS = int(os.environ.get("URGENCY_MIN_RELIEVERS", "2"))
URGENCY_EXIT_RUN_DIFF = int(os.environ.get("URGENCY_EXIT_RUN_DIFF", "4"))

# In-memory only: last (inning, half) seen per game, for half-inning change
# detection. After a restart the baseline is re-seeded without firing, same
# pattern as _last_pitcher_count.
_last_half_inning = {}

# --- Game-window scheduling ---
# The bot used to poll 24/7, which burned API calls and pushed "0 live"
# summaries all night. Instead it now sleeps outside the day's game window:
# wake shortly before first pitch, poll every POLL_INTERVAL_SECONDS until
# every game on the slate is Final, then sleep until tomorrow's first pitch.
SCHEDULE_ENABLED = os.environ.get("SCHEDULE_ENABLED", "true").strip().lower() not in (
    "0", "false", "no", "off",
)
# Wake this far before first pitch so the first poll lands on a live game
# rather than arriving late.
PREGAME_LEAD_SECONDS = int(os.environ.get("PREGAME_LEAD_SECONDS", "300"))
# While sleeping, still write status.json this often. Without a heartbeat a
# long sleep would let last_checked go stale and the dashboard would report
# the bot as dead when it is deliberately idle.
SLEEP_HEARTBEAT_SECONDS = int(os.environ.get("SLEEP_HEARTBEAT_SECONDS", "60"))
# How many days ahead to look for the next slate before giving up and
# re-checking later (covers the All-Star break and the gap before playoffs).
SCHEDULE_LOOKAHEAD_DAYS = 8

_bot_state = "scanning"
_next_wake_utc = None
_current_window = None

# In-memory only (not persisted across restarts): last known pitcher-used
# count per (game_pk, trailing_side), used to detect a fresh substitution.
_last_pitcher_count = {}


def _now_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _format_inning_line(inning, inning_ordinal, inning_state, outs):
    if inning_state and inning_ordinal:
        line = f"{inning_state} {inning_ordinal}"
    elif inning is not None:
        line = f"Inning {inning}"
    else:
        line = "Inning unknown"
    if outs is not None:
        line += f", {outs} out{'s' if outs != 1 else ''}"
    return line


def _lead_bucket(lead):
    """Largest BIG_LEAD_STEP-aligned milestone at or below `lead`, starting at
    BIG_LEAD_THRESHOLD. None if the lead hasn't reached the threshold yet.
    E.g. threshold=6, step=3: lead 6-8 -> 6, lead 9-11 -> 9, lead 12-14 -> 12."""
    if lead < BIG_LEAD_THRESHOLD:
        return None
    return BIG_LEAD_THRESHOLD + BIG_LEAD_STEP * ((lead - BIG_LEAD_THRESHOLD) // BIG_LEAD_STEP)


def _extreme_lead_bucket(lead):
    """Same shape as _lead_bucket but on the extreme-lead ladder.
    E.g. threshold=10, step=2: lead 10-11 -> 10, lead 12-13 -> 12, ..."""
    if lead < EXTREME_LEAD_THRESHOLD:
        return None
    return EXTREME_LEAD_THRESHOLD + EXTREME_LEAD_STEP * ((lead - EXTREME_LEAD_THRESHOLD) // EXTREME_LEAD_STEP)


def _trailing_side(linescore):
    """Which side is losing right now, or None if tied."""
    home_runs = linescore.get("home", 0)
    away_runs = linescore.get("away", 0)
    if home_runs == away_runs:
        return None
    return "away" if home_runs > away_runs else "home"


def _relievers_used(boxscore, side):
    """Relievers a side has gone to, excluding the starter."""
    return max(0, mlb_api.count_pitchers_used(boxscore, side) - 1)


def _in_urgency(game_pk, alerted):
    return f"urgency:{game_pk}" in alerted


def _bullpen_state_line(boxscore, side):
    """One-line summary of a side's current pitching situation: who's on the
    mound, their line, and how deep into the bullpen the team already is.
    Shared by every urgency-mode notification so each ping is self-contained."""
    pen = mlb_api.get_bullpen_detail(boxscore, side)
    if not pen:
        return None
    current = pen[-1]
    bits = [current["name"]]
    if current.get("innings_pitched") is not None:
        bits.append(f"{current['innings_pitched']} IP")
    if current.get("pitches") is not None:
        bits.append(f"{current['pitches']} pitches")
    return f"On the mound: {' - '.join(bits)} ({max(0, len(pen) - 1)} reliever(s) used)"


def check_urgency_mode(game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs, alerted):
    """Latch a game into (or out of) urgency mode and announce the transition.

    Entry: lead >= URGENCY_RUN_DIFF AND the trailing team has already used
    URGENCY_MIN_RELIEVERS+ relievers. Exit: lead falls below
    URGENCY_EXIT_RUN_DIFF (hysteresis gap prevents flapping).

    Entry state is stored in the persisted `alerted` set so a bot restart
    mid-game doesn't re-announce a mode the game is already in.

    Returns True only on the poll that enters the mode. The entry message
    already names the incoming pitcher, so check_pitcher_change() uses this
    to skip its own push for the same substitution."""
    trailing = _trailing_side(linescore)
    already_in = _in_urgency(game_pk, alerted)
    key = f"urgency:{game_pk}"

    if trailing is None:
        # Tied. Only meaningful if we were previously in the mode.
        if already_in:
            alerted.discard(key)
            notifier.send_alert(
                "Urgency Mode Off",
                f"{linescore['away_name']} {linescore['away']} - "
                f"{linescore['home_name']} {linescore['home']}\n\n"
                "Game is tied. No longer tracking every inning/pitcher change.",
                priority="default",
                tags="mute",
            )
        return

    leading = "home" if trailing == "away" else "away"
    lead = abs(linescore["home"] - linescore["away"])
    relievers = _relievers_used(boxscore, trailing)
    leading_team = linescore[f"{leading}_name"]
    trailing_team = linescore[f"{trailing}_name"]

    if already_in:
        if lead < URGENCY_EXIT_RUN_DIFF:
            alerted.discard(key)
            notifier.send_alert(
                "Urgency Mode Off",
                f"{leading_team} {linescore[leading]} - {trailing_team} {linescore[trailing]}\n\n"
                f"Lead is back down to {lead}. No longer tracking every inning/pitcher change.",
                priority="default",
                tags="mute",
            )
        return

    if lead < URGENCY_RUN_DIFF or relievers < URGENCY_MIN_RELIEVERS:
        return

    alerted.add(key)
    inning_line = _format_inning_line(inning, inning_ordinal, inning_state, outs)
    parts = [
        f"URGENCY MODE: {leading_team} by {lead}, {trailing_team} bullpen is going.",
        f"{leading_team} {linescore[leading]} - {trailing_team} {linescore[trailing]}",
        inning_line,
        "",
        f"{trailing_team} has used {relievers} reliever(s).",
    ]
    pen_line = _bullpen_state_line(boxscore, trailing)
    if pen_line:
        parts.append(pen_line)
    parts.append("")
    parts.append("Now pinging on EVERY half-inning and EVERY pitcher change in this game.")

    notifier.send_alert(
        "Urgency Mode ON",
        "\n".join(parts),
        priority="urgent",
        tags="rotating_light,stopwatch",
    )
    return True


def check_inning_change(game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs, alerted, suppress=False):
    """Urgency-mode-only: ping on every half-inning transition. Fires on Top
    and Bottom only -- MLB also reports "Middle"/"End" between halves, which
    would double the notification volume without adding information."""
    if inning is None or inning_state not in ("Top", "Bottom"):
        return

    marker = (inning, inning_state)
    baseline = _last_half_inning.get(game_pk)
    _last_half_inning[game_pk] = marker

    # Unchanged, or first sighting after a restart -- re-seed without firing.
    if baseline is None or baseline == marker:
        return
    if not _in_urgency(game_pk, alerted):
        return
    # Mode-entry message already carries the score/inning/bullpen state --
    # baseline is advanced above, so skipping here costs no future ping.
    if suppress:
        return

    trailing = _trailing_side(linescore)
    if trailing is None:
        return
    leading = "home" if trailing == "away" else "away"
    lead = abs(linescore["home"] - linescore["away"])

    parts = [
        f"{linescore[f'{leading}_name']} {linescore[leading]} - "
        f"{linescore[f'{trailing}_name']} {linescore[trailing]} (lead {lead})",
        _format_inning_line(inning, inning_ordinal, inning_state, outs),
    ]
    pen_line = _bullpen_state_line(boxscore, trailing)
    if pen_line:
        parts.append("")
        parts.append(f"{linescore[f'{trailing}_name']} - {pen_line}")

    notifier.send_alert(
        f"Inning Change - {inning_state} {inning_ordinal or inning}",
        "\n".join(parts),
        priority="high",
        tags="stopwatch",
    )


def check_big_lead(game_pk, linescore, inning, inning_ordinal, inning_state, outs, alerted):
    """Score-only watch tier, completely independent of who's pitching: fires
    the first time a game's run differential crosses BIG_LEAD_THRESHOLD, and
    again every BIG_LEAD_STEP runs after that as the lead keeps growing. Also
    marks the game as "big lead flagged" (via the alerted key itself) so
    check_pitcher_change() knows to start watching it."""
    home_runs = linescore.get("home", 0)
    away_runs = linescore.get("away", 0)
    lead = abs(home_runs - away_runs)
    # Extreme tier handles anything above its threshold at tighter increments
    # -- don't double-alert the same game from Big Lead at bucket 12/15/etc.
    if lead >= EXTREME_LEAD_THRESHOLD:
        return
    bucket = _lead_bucket(lead)
    if bucket is None:
        return

    key = f"biglead:{game_pk}:{bucket}"
    if key in alerted:
        return
    alerted.add(key)

    if home_runs > away_runs:
        leading_side, trailing_side = "home", "away"
    else:
        leading_side, trailing_side = "away", "home"
    leading_team = linescore[f"{leading_side}_name"]
    trailing_team = linescore[f"{trailing_side}_name"]
    leading_runs = linescore[leading_side]
    trailing_runs = linescore[trailing_side]

    inning_line = _format_inning_line(inning, inning_ordinal, inning_state, outs)
    message = (
        f"{leading_team} {leading_runs} - {trailing_team} {trailing_runs}\n"
        f"{inning_line}\n\n"
        f"Lead is up to {lead} runs. Worth watching for a bullpen move."
    )

    try:
        all_odds = odds.get_cached_or_fetch()
        if all_odds:
            match = odds.find_game(all_odds, linescore["home_name"], linescore["away_name"])
            game_odds = odds.extract_for_book(match, None, odds.BOOK) if match else None
            if game_odds:
                ladder = game_odds.get(f"spreads_{leading_side}") or []
                if ladder:
                    point = ladder[0]["point"]
                    price = ladder[0]["price"]
                    message += (
                        f"\nCurrent {game_odds.get('book_title', 'book')} line: "
                        f"{leading_team} {point:+g} ({price:+d})"
                    )
    except Exception as e:
        print(f"[bot] Big-lead odds lookup failed for {game_pk}: {e}")

    notifier.send_alert("Big Lead Watch", message, priority="default", tags="eyes")


def check_extreme_lead(game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs, alerted):
    """Once a lead reaches EXTREME_LEAD_THRESHOLD, alert at urgent priority and
    re-alert on tighter EXTREME_LEAD_STEP increments. Also inlines the losing
    team's current bullpen state (# relievers already used, current pitcher's
    pitch count) so the notification itself signals how close a bullpen change
    might be. Marks the game "extreme flagged" via its own alerted key so
    check_pitcher_change() picks up the higher-urgency treatment."""
    home_runs = linescore.get("home", 0)
    away_runs = linescore.get("away", 0)
    lead = abs(home_runs - away_runs)
    bucket = _extreme_lead_bucket(lead)
    if bucket is None:
        return

    key = f"extreme:{game_pk}:{bucket}"
    if key in alerted:
        return
    alerted.add(key)

    if home_runs > away_runs:
        leading_side, trailing_side = "home", "away"
    else:
        leading_side, trailing_side = "away", "home"
    leading_team = linescore[f"{leading_side}_name"]
    trailing_team = linescore[f"{trailing_side}_name"]
    leading_runs = linescore[leading_side]
    trailing_runs = linescore[trailing_side]

    bullpen = mlb_api.get_bullpen_detail(boxscore, trailing_side)
    relievers_used = max(0, len(bullpen) - 1)  # exclude starter
    current = bullpen[-1] if bullpen else None
    current_line = ""
    if current:
        pieces = [current["name"]]
        if current.get("innings_pitched") is not None:
            pieces.append(f"{current['innings_pitched']} IP")
        if current.get("pitches") is not None:
            pieces.append(f"{current['pitches']} pitches")
        current_line = " — ".join(pieces)

    inning_line = _format_inning_line(inning, inning_ordinal, inning_state, outs)
    message_parts = [
        f"EXTREME LEAD: {leading_team} up by {lead}.",
        f"{leading_team} {leading_runs} - {trailing_team} {trailing_runs}",
        inning_line,
        "",
        f"{trailing_team} bullpen: {relievers_used} reliever(s) burned already.",
    ]
    if current_line:
        message_parts.append(f"On the mound: {current_line}")
    message_parts.append("")
    message_parts.append("Watch this game closely — every bullpen swap will ping.")

    try:
        all_odds = odds.get_cached_or_fetch()
        if all_odds:
            match = odds.find_game(all_odds, linescore["home_name"], linescore["away_name"])
            game_odds = odds.extract_for_book(match, None, odds.BOOK) if match else None
            if game_odds:
                ladder = game_odds.get(f"spreads_{leading_side}") or []
                if ladder:
                    point = ladder[0]["point"]
                    price = ladder[0]["price"]
                    message_parts.append(
                        f"Current {game_odds.get('book_title', 'book')} line: "
                        f"{leading_team} {point:+g} ({price:+d})"
                    )
    except Exception as e:
        print(f"[bot] Extreme-lead odds lookup failed for {game_pk}: {e}")

    notifier.send_alert(
        "Extreme Lead Alert",
        "\n".join(message_parts),
        priority="urgent",
        tags="rotating_light,fire",
    )


def check_pitcher_change(game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs, alerted, suppress=False):
    """Urgent, early-warning tier: once a game has been big-lead-flagged (by
    check_big_lead above), ping on EVERY new pitcher for the trailing team --
    reliever-to-reliever swaps included -- so bullpen churn is visible before
    the eventual position-player move, not just at the moment it happens. If
    the new arm IS a position player, this stays quiet and lets the existing
    Blowout/Bullpen-Exhausted tiers handle it instead, so the same event
    doesn't fire twice."""
    home_runs = linescore.get("home", 0)
    away_runs = linescore.get("away", 0)
    if home_runs == away_runs:
        return  # no trailing team to watch

    trailing_side = "away" if home_runs > away_runs else "home"
    current_count = mlb_api.count_pitchers_used(boxscore, trailing_side)
    state_key = (game_pk, trailing_side)
    baseline = _last_pitcher_count.get(state_key)

    if baseline is None or current_count <= baseline:
        _last_pitcher_count[state_key] = current_count
        return

    _last_pitcher_count[state_key] = current_count

    # Baseline is advanced above before bailing out, so a suppressed change
    # doesn't re-fire on the next poll.
    if suppress:
        return

    is_urgency_game = _in_urgency(game_pk, alerted)
    is_extreme_game = any(k.startswith(f"extreme:{game_pk}:") for k in alerted)
    is_armed = (
        is_urgency_game
        or is_extreme_game
        or any(k.startswith(f"biglead:{game_pk}:") for k in alerted)
    )
    if not is_armed:
        return

    team = boxscore["teams"][trailing_side]
    pitcher_ids = team.get("pitchers", [])
    if not pitcher_ids:
        return
    new_pitcher = team.get("players", {}).get(f"ID{pitcher_ids[-1]}")
    if not new_pitcher:
        return
    position_abbr = new_pitcher.get("position", {}).get("abbreviation")
    if position_abbr not in ("P", "TWP"):
        # A position player just entered -- that's the Blowout/Bullpen-
        # Exhausted tier's job. Don't double-alert the same event.
        return

    trailing_team = linescore[f"{trailing_side}_name"]
    leading_side = "home" if trailing_side == "away" else "away"
    leading_team = linescore[f"{leading_side}_name"]
    pitcher_name = new_pitcher.get("person", {}).get("fullName", "New pitcher")
    inning_line = _format_inning_line(inning, inning_ordinal, inning_state, outs)

    # Urgency mode is the explicit "watch this game" state, so it owns the
    # label when active; extreme is a lead descriptor that still gets noted.
    if is_urgency_game:
        title = "Urgency Mode - Pitcher Change"
        header = "EXTREME LEAD - " if is_extreme_game else "URGENCY - "
        footer = "Bullpen is emptying -- position player could be next."
        tags = "rotating_light,stopwatch"
    elif is_extreme_game:
        title = "Pitcher Change Alert (Extreme)"
        header = "EXTREME LEAD - "
        footer = "Extreme blowout still active -- position player could be next."
        tags = "rotating_light,fire"
    else:
        title = "Pitcher Change Alert"
        header = ""
        footer = "Bullpen still churning in this blowout -- watch for a position player next."
        tags = "warning"

    message = (
        f"{header}{leading_team} {linescore[leading_side]} - {trailing_team} {linescore[trailing_side]}\n"
        f"{inning_line}\n\n"
        f"{pitcher_name} is in for {trailing_team} (reliever #{current_count}). "
        f"{footer}"
    )
    notifier.send_alert(title, message, priority="urgent", tags=tags)


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
    linescore = mlb_api.get_linescore(boxscore)
    detail = mlb_api.get_linescore_detail(game_pk)
    inning = detail.get("inning")
    inning_ordinal = detail.get("inning_ordinal")
    inning_state = detail.get("inning_state")
    outs = detail.get("outs")

    # Order matters. Urgency mode latches first so the inning/pitcher checks
    # below see the current mode state. Extreme runs before big-lead because
    # big-lead self-suppresses inside extreme territory, keeping the two from
    # double-alerting on overlapping buckets.
    entered_urgency = check_urgency_mode(
        game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs, alerted
    )
    check_extreme_lead(game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs, alerted)
    check_big_lead(game_pk, linescore, inning, inning_ordinal, inning_state, outs, alerted)
    check_inning_change(
        game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs, alerted,
        suppress=bool(entered_urgency),
    )
    check_pitcher_change(
        game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs, alerted,
        suppress=bool(entered_urgency),
    )

    hits = mlb_api.find_position_players_pitching(boxscore)
    if not hits:
        return

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

        inning_line = _format_inning_line(inning, inning_ordinal, inning_state, outs)

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


def _iso_or_none(dt):
    return dt.isoformat() if dt else None


def _schedule_block():
    """The `schedule` object the dashboard reads out of status.json. Returns
    None when scheduling is off or no window has been computed yet, which the
    dashboard treats as "hide the state indicator"."""
    if not _current_window:
        return None
    return {
        "date": _current_window.get("date"),
        "games_today": _current_window.get("game_count", 0),
        "games_final": _current_window.get("games_final", 0),
        "first_pitch": _iso_or_none(_current_window.get("first_pitch_utc")),
        "last_scheduled_start": _iso_or_none(_current_window.get("last_scheduled_start_utc")),
        "next_wake": _iso_or_none(_next_wake_utc),
    }


def _write_status(live_game_count):
    import json

    with open(paths.data_path("status.json"), "w") as f:
        json.dump(
            {
                "last_checked": _now_iso(),
                "live_games": live_game_count,
                "bot_state": _bot_state,
                "schedule": _schedule_block(),
                "blowout_run_diff": BLOWOUT_RUN_DIFF,
                "min_inning": MIN_INNING,
                "run_diff_mid": RUN_DIFF_MID,
                "run_diff_late": RUN_DIFF_LATE,
                "bullpen_exhaustion_count": BULLPEN_EXHAUSTION_COUNT,
                "big_lead_threshold": BIG_LEAD_THRESHOLD,
                "big_lead_step": BIG_LEAD_STEP,
                "extreme_lead_threshold": EXTREME_LEAD_THRESHOLD,
                "extreme_lead_step": EXTREME_LEAD_STEP,
                "urgency_run_diff": URGENCY_RUN_DIFF,
                "urgency_min_relievers": URGENCY_MIN_RELIEVERS,
                "urgency_exit_run_diff": URGENCY_EXIT_RUN_DIFF,
                "poll_interval_seconds": POLL_INTERVAL_SECONDS,
            },
            f,
            indent=2,
        )


def _write_live_snapshot(game_pks, alerted=None):
    """Write live_snapshot.json: the WHOLE day's slate, not just live games, so
    the dashboard can show upcoming and final games alongside in-progress ones.

    Cost control -- the expensive calls are per-game and only made where they
    buy something:
      * boxscore + linescore (2 requests each): live games only. An upcoming
        game has no bullpen or count to report, and a final one is settled.
      * alternate run-line ladder (1 odds credit each): live games only.
    Scheduled and final games are filled from the schedule payload, which is a
    single cached request for the entire slate, plus the shared odds snapshot
    that has already been fetched. So adding the full slate costs nothing
    beyond what live games already cost.
    """
    import json

    live = set(game_pks or [])
    payload = {
        "fetched_at": time.time(),
        "book": odds.BOOK,
        "quota": odds.quota_status() if live else None,
        "games": [],
    }

    all_odds = odds.get_cached_or_fetch() if live else None
    # quota_status() reflects the fetch that may have just happened above
    if live:
        payload["quota"] = odds.quota_status()

    # Whole slate when scheduling is available; otherwise fall back to the
    # live pks so the dashboard still works with SCHEDULE_ENABLED=false.
    slate = []
    if schedule is not None:
        try:
            slate = schedule.get_day_games(schedule.today_et())
        except Exception as e:
            print(f"[bot] Slate lookup failed, falling back to live only: {e}")
    if not slate:
        slate = [{"game_pk": pk} for pk in live]

    for entry in slate:
        pk = entry.get("game_pk")
        if entry.get("is_postponed"):
            continue
        try:
            is_live = pk in live
            status = entry.get("status") or ("Live" if is_live else None)
            game = {
                "game_pk": pk,
                "status": status,                       # Preview | Live | Final
                "detailed_state": entry.get("detailed_state"),
                "is_live": is_live,
                "start_utc": entry["start_utc"].isoformat() if entry.get("start_utc") else None,
                "away_team": entry.get("away"),
                "home_team": entry.get("home"),
                "away_id": entry.get("away_id"),        # drives the logo URL
                "home_id": entry.get("home_id"),
                "away_record": entry.get("away_record"),
                "home_record": entry.get("home_record"),
                "away_runs": entry.get("away_score") or 0,
                "home_runs": entry.get("home_score") or 0,
                "inning": None, "inning_ordinal": None, "inning_state": None,
                "balls": None, "strikes": None, "outs": None,
                "away_bullpen": [], "home_bullpen": [],
                "odds": None,
                "urgency": bool(alerted) and _in_urgency(pk, alerted),
            }

            if is_live:
                box = mlb_api.get_boxscore(pk)
                detail = mlb_api.get_linescore_detail(pk)
                game["home_team"] = box["teams"]["home"]["team"]["name"]
                game["away_team"] = box["teams"]["away"]["team"]["name"]
                game.update({
                    "home_runs": detail.get("home_runs", 0),
                    "away_runs": detail.get("away_runs", 0),
                    "inning": detail.get("inning"),
                    "inning_ordinal": detail.get("inning_ordinal"),
                    "inning_state": detail.get("inning_state"),
                    "balls": detail.get("balls"),
                    "strikes": detail.get("strikes"),
                    "outs": detail.get("outs"),
                    "home_bullpen": mlb_api.get_bullpen_detail(box, "home"),
                    "away_bullpen": mlb_api.get_bullpen_detail(box, "away"),
                })

            if all_odds and game["home_team"] and game["away_team"]:
                match = odds.find_game(all_odds, game["home_team"], game["away_team"])
                if match:
                    # Alternate ladders cost a credit each -- live games only.
                    alt = odds.get_alternates_for_event(match.get("id")) if is_live else None
                    game["odds"] = odds.extract_for_book(match, alt, odds.BOOK)

            payload["games"].append(game)
        except Exception as e:
            print(f"[bot] Snapshot error for game {pk}: {e}")

    with open(paths.data_path("live_snapshot.json"), "w") as f:
        json.dump(payload, f, indent=2)

    return payload


def _maybe_send_hourly_summary(games):
    """Push a slate-wide digest -- every live game, ranked biggest lead to
    smallest (same ordering as the dashboard) -- on a rolling interval,
    independent of whether any individual alert has fired. Lets you glance
    at your phone instead of the dashboard to see where the whole slate
    stands."""
    global _last_summary_at
    now = time.time()
    if now - _last_summary_at < SUMMARY_INTERVAL_SECONDS:
        return
    _last_summary_at = now

    if not games:
        message = "No live MLB games right now."
    else:
        ranked = sorted(
            games,
            key=lambda g: abs((g.get("home_runs") or 0) - (g.get("away_runs") or 0)),
            reverse=True,
        )
        lines = []
        for g in ranked:
            lead = abs((g.get("home_runs") or 0) - (g.get("away_runs") or 0))
            inning_bit = g.get("inning_ordinal") or g.get("inning") or "?"
            state_bit = g.get("inning_state") or ""
            lines.append(
                f"{g.get('away_team')} {g.get('away_runs', 0)} - "
                f"{g.get('home_team')} {g.get('home_runs', 0)} "
                f"({state_bit} {inning_bit}, lead {lead})".replace("  ", " ")
            )
        message = "\n".join(lines)

    title = f"Hourly Slate Summary ({len(games)} live)"
    notifier.send_alert(title, message, priority="default", tags="bar_chart")


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
    snapshot_payload = _write_live_snapshot(game_pks, alerted)
    # The snapshot now carries the whole slate; the summary is about live games
    # only, so filter rather than reporting scheduled games as in progress.
    live_only = [g for g in snapshot_payload.get("games", []) if g.get("is_live")]
    _maybe_send_hourly_summary(live_only)


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _write_empty_snapshot():
    """Blank out live_snapshot.json when going to sleep so the dashboard shows
    an empty slate instead of the last finished games frozen in place."""
    import json

    try:
        with open(paths.data_path("live_snapshot.json"), "w") as f:
            json.dump(
                {"fetched_at": time.time(), "book": odds.BOOK, "quota": None, "games": []},
                f,
                indent=2,
            )
    except Exception as e:
        print(f"[bot] Could not clear live snapshot: {e}")


# Real MLB first pitches land roughly 15:00Z (11am ET) through 02:30Z (10:30pm
# ET next day). Anything starting inside this band is not a real start time --
# it's a TBD placeholder. The whole postseason is scheduled at 07:33Z with
# "AL Higher Seed"-style names until matchups are set, and a scheduler that
# trusted those would wake at 3:33am, find nothing, and sleep through the
# actual 8pm games.
PLACEHOLDER_START_HOURS = range(3, 14)  # UTC


def _is_placeholder_window(window):
    first = window.get("first_pitch_utc")
    return bool(first) and first.hour in PLACEHOLDER_START_HOURS


def _next_slate_start(after_date_str):
    """First pitch of the next day that actually has games, searching forward
    from the day AFTER after_date_str. Returns (datetime, date_str) or
    (None, None) if nothing is scheduled within the lookahead."""
    from datetime import date, timedelta

    start = date.fromisoformat(after_date_str)
    for offset in range(1, SCHEDULE_LOOKAHEAD_DAYS + 1):
        day = (start + timedelta(days=offset)).isoformat()
        try:
            w = schedule.compute_window(day)
        except Exception as e:
            print(f"[bot] Schedule lookahead failed for {day}: {e}")
            continue
        if w.get("game_count") and w.get("first_pitch_utc"):
            if _is_placeholder_window(w):
                # TBD postseason time -- don't trust it. Wake at 14:00Z
                # (10am ET), safely ahead of any real first pitch, and let
                # the placeholder branch in run_scheduled keep us awake.
                from datetime import datetime, time as _time, timezone

                return (
                    datetime.combine(
                        date.fromisoformat(day), _time(14, 0), tzinfo=timezone.utc
                    ),
                    day,
                )
            return w["first_pitch_utc"], day
    return None, None


def _sleep_until(target_utc, reason):
    """Idle until target_utc, writing a status heartbeat every
    SLEEP_HEARTBEAT_SECONDS so the dashboard can tell deliberate sleep apart
    from a crashed process."""
    global _bot_state, _next_wake_utc

    _bot_state = "sleeping"
    _next_wake_utc = target_utc
    _write_empty_snapshot()

    mins = (target_utc - _utcnow()).total_seconds() / 60
    print(f"[bot] Sleeping {mins:.0f} min ({reason}). Waking at {target_utc.isoformat()}.")

    while True:
        remaining = (target_utc - _utcnow()).total_seconds()
        if remaining <= 0:
            break
        _write_status(0)
        time.sleep(min(SLEEP_HEARTBEAT_SECONDS, remaining))

    _bot_state = "scanning"
    _next_wake_utc = None


def run_scheduled(alerted):
    """Main loop when SCHEDULE_ENABLED. Sleeps outside the day's game window
    and polls every POLL_INTERVAL_SECONDS inside it.

    Day boundaries come from the API's `officialDate`, which is Eastern-time
    based, so a game starting 01:05 UTC still belongs to the previous day's
    slate and keeps the bot awake rather than being treated as tomorrow."""
    global _current_window, _bot_state

    from datetime import timedelta

    while True:
        today = schedule.today_et()
        try:
            window = schedule.compute_window(today)
        except Exception as e:
            print(f"[bot] Schedule lookup failed ({e}); polling once and retrying.")
            run_once(alerted)
            state.save_alerted(alerted)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        _current_window = window
        now = _utcnow()
        first = window.get("first_pitch_utc")
        backstop = window.get("backstop_utc")

        # Slate is over (or empty) -- sleep to the next day that has games.
        if not window.get("game_count") or window.get("all_final"):
            reason = "no games today" if not window.get("game_count") else "slate complete"
            target, target_day = _next_slate_start(today)
            if target is None:
                # Nothing scheduled within the lookahead; re-check in 6 hours.
                target = now + timedelta(hours=6)
                reason += ", nothing scheduled ahead"
            else:
                target -= timedelta(seconds=PREGAME_LEAD_SECONDS)
                reason += f", next slate {target_day}"
            _sleep_until(target, reason)
            continue

        # Postseason days carry placeholder start times until matchups are
        # set. Their window is meaningless, so poll straight through the day
        # rather than risk sleeping past a real game. Costs a day of extra
        # API calls; never misses a playoff blowout.
        if _is_placeholder_window(window):
            _bot_state = "scanning"
            try:
                run_once(alerted)
                state.save_alerted(alerted)
            except Exception as e:
                print(f"[bot] Poll error: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # Games today but first pitch hasn't landed yet.
        if first:
            wake_at = first - timedelta(seconds=PREGAME_LEAD_SECONDS)
            if now < wake_at:
                _sleep_until(wake_at, f"first pitch {first.isoformat()}")
                continue

        # Games never went Final (suspended/stuck) -- give up on the day.
        if backstop and now > backstop:
            target, target_day = _next_slate_start(today)
            if target is None:
                target = now + timedelta(hours=6)
            else:
                target -= timedelta(seconds=PREGAME_LEAD_SECONDS)
            _sleep_until(target, f"past backstop, next slate {target_day}")
            continue

        # Inside the window: normal polling.
        _bot_state = "scanning"
        try:
            run_once(alerted)
            state.save_alerted(alerted)
        except Exception as e:
            print(f"[bot] Poll error: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


def run_unscheduled(alerted):
    """Legacy 24/7 loop, used when SCHEDULE_ENABLED is off or schedule.py is
    unavailable."""
    while True:
        try:
            run_once(alerted)
            state.save_alerted(alerted)
        except Exception as e:
            print(f"[bot] Poll error: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


def _ensure_season_schedule(max_age_hours=24):
    """Refresh the cached season schedule if it is missing or stale. Purely
    informational -- the poll loop always asks the live API for the current
    day -- but it means the deployed instance holds the full remaining season
    rather than only whatever day it happens to be. One ranged request.
    Never fatal: a failure here must not stop the bot from watching games."""
    from datetime import datetime, timezone

    try:
        existing = schedule.load_season()
        fetched_at = (existing or {}).get("fetched_at")
        if fetched_at:
            # schedule.py writes this as an ISO string, not an epoch float.
            fetched_dt = datetime.fromisoformat(fetched_at)
            age_h = (datetime.now(timezone.utc) - fetched_dt).total_seconds() / 3600
            if age_h < max_age_hours:
                days = existing.get("days", {})
                games = sum(d.get("game_count", 0) for d in days.values())
                print(f"[bot] Season schedule cached ({games} games, {age_h:.1f}h old).")
                return
        season = schedule.fetch_season()
        days = season.get("days", {})
        games = sum(d.get("game_count", 0) for d in days.values())
        print(f"[bot] Season schedule refreshed: {games} games across {len(days)} days.")
    except Exception as e:
        print(f"[bot] Season schedule refresh failed (non-fatal): {e}")


def run_loop(alerted):
    """Entrypoint shared by bot.py standalone and app.py's background thread."""
    if SCHEDULE_ENABLED and schedule is not None:
        _ensure_season_schedule()
        run_scheduled(alerted)
    else:
        why = "SCHEDULE_ENABLED=false" if schedule is not None else "schedule.py unavailable"
        print(f"[bot] Game-window scheduling off ({why}); polling 24/7.")
        run_unscheduled(alerted)


def notify_startup():
    """Fire a low-key confirmation push every time the bot process starts, so
    activating it (either `python bot.py` or `python app.py`) gives immediate
    proof of life instead of trusting a silent background process. Distinct
    from real alerts: plain title, "default" priority, checkmark tag."""
    title = "Bot Started"
    message = (
        "MLB Blowout Bot is online and sweeping today's live games.\n"
        f"Blowout tier: inning>={MIN_INNING}, {RUN_DIFF_MID}+ runs (innings {MIN_INNING}-6) / "
        f"{RUN_DIFF_LATE}+ runs (7+).\n"
        f"Bullpen-exhausted tier: {BULLPEN_EXHAUSTION_COUNT}+ prior relievers (any inning).\n"
        f"Big Lead Watch: {BIG_LEAD_THRESHOLD}+ run lead, any inning, re-alerts every "
        f"{BIG_LEAD_STEP} runs.\n"
        f"Extreme Lead Alert: {EXTREME_LEAD_THRESHOLD}+ run lead, re-alerts every "
        f"{EXTREME_LEAD_STEP} runs at urgent priority.\n"
        f"Pitcher Change Alert: urgent ping on any new reliever once a game is big-lead "
        f"flagged.\n"
        f"Urgency Mode: {URGENCY_RUN_DIFF}+ lead AND {URGENCY_MIN_RELIEVERS}+ relievers used "
        f"-> pings every half-inning and every pitcher change (exits below "
        f"{URGENCY_EXIT_RUN_DIFF}).\n"
        f"Polling every {POLL_INTERVAL_SECONDS}s."
    )
    if SCHEDULE_ENABLED and schedule is not None:
        try:
            w = schedule.compute_window(schedule.today_et())
            if w.get("game_count"):
                first = w.get("first_pitch_utc")
                message += (
                    f"\n\nToday: {w['game_count']} game(s)."
                    + (f" First pitch {first.isoformat()}." if first else "")
                    + " Sleeping outside the window."
                )
            else:
                message += "\n\nNo games today -- sleeping until the next slate."
        except Exception as e:
            message += f"\n\nSchedule lookup failed at startup: {e}"
    notifier.send_alert(title, message, priority="default", tags="white_check_mark")


def main():
    print(
        f"MLB Blowout Bot starting. Blowout tier: inning>={MIN_INNING}, "
        f"{RUN_DIFF_MID}+ runs (innings {MIN_INNING}-6) / {RUN_DIFF_LATE}+ runs (7+). "
        f"Bullpen-exhausted tier: {BULLPEN_EXHAUSTION_COUNT}+ prior relievers (any inning). "
        f"Polling every {POLL_INTERVAL_SECONDS}s."
    )
    notify_startup()
    alerted = state.load_alerted()
    try:
        run_loop(alerted)
    except KeyboardInterrupt:
        print("\n[bot] Stopped.")
        state.save_alerted(alerted)


if __name__ == "__main__":
    main()
