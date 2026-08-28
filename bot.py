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
    POLL_INTERVAL_SECONDS    how often to poll live games (default 30)
    FAST_POLL_INTERVAL_SECONDS  tightened interval while a game is hot (default 15)
    RUN_DIFF_MID              run diff needed in innings 1-6 (default 6)
    RUN_DIFF_LATE             run diff needed in innings 7+ (default 4)
    BULLPEN_EXHAUSTION_COUNT  relievers used by the trailing team that fires the
                              "Bullpen Exhausted" ping at high priority (default 3)
    BULLPEN_CRITICAL_COUNT    relievers used that escalates the same ping to urgent
                              (default 4)
"""
import os
import time

from dotenv import load_dotenv

load_dotenv()

import alert_log
import bullpen
import mlb_api
import notifier
import odds
import paths
import state

try:
    import schedule
except ImportError:  # scheduling is optional -- fall back to 24/7 polling
    schedule = None

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))

# --- Adaptive polling ---
# The poll interval IS the detection floor: a position player can be on the
# mound for up to one interval before the bot sees them. When a game reaches a
# state where that substitution is plausibly next, the loop tightens to
# FAST_POLL_INTERVAL_SECONDS and stays there until no game qualifies -- which
# in practice means until that game ends.
#
# This costs nothing in odds credits: odds are TTL-cached (300s main, 900s
# alternates) independent of the loop, so only the free MLB Stats API sees the
# extra calls.
FAST_POLL_INTERVAL_SECONDS = int(os.environ.get("FAST_POLL_INTERVAL_SECONDS", "15"))
_fast_poll_active = False   # for transition logging + status.json

# --- Rule engine: run-differential only, NO inning floor ---
# There is deliberately no minimum inning. The bet is that the winning side's
# spread WIDENS from the moment of concession, so an early concession leaves
# MORE innings for that to happen, not fewer -- gating on inning would silence
# the most valuable version of the signal.
#
# RUN_DIFF_MID/LATE still scale the required differential DOWN as the game gets
# later (a 4-run lead in the 9th is as conceded as a 6-run lead in the 3rd).
# That is a relaxation, never a floor.
RUN_DIFF_MID = int(os.environ.get("RUN_DIFF_MID", "6"))    # innings 1-6
RUN_DIFF_LATE = int(os.environ.get("RUN_DIFF_LATE", "4"))                  # innings 7+

# --- Bullpen depth ladder ---
# Bullpen depth is a primary signal in its own right, not just a modifier on
# the golden alert: a trailing team burning arm after arm is walking toward the
# position-player move. Two rungs, each pushing once per game per side, on
# reliever count ALONE -- no run-diff or inning gate:
#
#   BULLPEN_EXHAUSTION_COUNT relievers -> "Bullpen Exhausted", high priority
#   BULLPEN_CRITICAL_COUNT   relievers -> same alert escalated to urgent
#
# The ladder ends there. Past the critical rung, ongoing churn is the pitcher-
# change tier's job, and the position player himself is TIER 0.
BULLPEN_EXHAUSTION_COUNT = int(os.environ.get("BULLPEN_EXHAUSTION_COUNT", "3"))
BULLPEN_CRITICAL_COUNT = int(os.environ.get("BULLPEN_CRITICAL_COUNT", "4"))

# How often (seconds) to push a slate-wide summary, independent of any
# individual alert. Rolling cadence from process start, not wall-clock-
# aligned (i.e. "every hour on the hour" would need different logic).

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

# In-memory only: the lead reported by the last inning report for a game, so
# the next one can say how far the lead moved between them.
_last_digest_lead = {}


# --- Notification ordering ---------------------------------------------------
# A phone's notification stack shows the MOST RECENT push on top, so whatever
# is sent LAST is what you actually see. That made send order the real priority
# system -- and until this ladder existed, send order was just the order the
# tiers happened to be called in inside check_game(). Two things went wrong as
# a direct result:
#
#   * the inning-change ping was called after the bullpen ladder, so the
#     low-value "it's the top of the 7th" push landed on top of the
#     bullpen-exhausted push it was supposed to sit under (user-reported), and
#   * the GOLDEN push was called first -- correctly, it must be computed before
#     anything that can fail -- which put the single most important alert this
#     bot produces at the BOTTOM of the stack.
#
# So ordering is now explicit data, not call position. Tiers no longer push;
# they describe an alert to the AlertBus, which sorts by this rank and emits in
# ASCENDING importance so the most important push is sent last and lands on top.
# Higher rank = more important = sent later = higher on the phone.
#
# Adding a tier means adding a rank here. Forgetting to is safe by construction:
# unranked alerts sort below everything, so a new tier can never bury a critical
# one no matter where its call is placed in check_game().
# Identifies the running build. Every deploy restarts the process, so a client
# that sees this value change knows the HTML and JS it loaded are stale. A
# long-lived phone tab polls JSON forever but never re-downloads the page
# itself, which is how the dashboard silently ran weeks-old code.
BUILD_ID = (
    os.environ.get("RAILWAY_GIT_COMMIT_SHA")
    or os.environ.get("RAILWAY_DEPLOYMENT_ID")
    or ""
)[:12] or f"local-{int(time.time())}"

# True only for the first poll after the process starts. See AlertBus.flush.
_seeding = True


def _seeding_poll_done():
    """Called once the first poll completes, so later polls alert normally."""
    global _seeding
    if _seeding:
        _seeding = False


ALERT_RANK = {
    "urgency_off": 10,        # informational, the game stopped being interesting
    "inning_digest": 20,      # routine per-inning traffic
    "big_lead": 30,
    "extreme_lead": 40,
    "urgency_on": 50,
    "pitcher_change": 60,     # bullpen is actually moving
    "bullpen_exhausted": 70,  # the ladder into TIER 0
    "bullpen_critical": 80,
    "golden": 100,            # TIER 0. Nothing outranks it, ever.
}
UNRANKED_ALERT_RANK = 0


class AlertBus:
    """Collects everything one poll of one game wants to say, then sends it in
    ascending ALERT_RANK order.

    Tiers call bus.add() instead of notifier.send_alert(). Nothing leaves the
    process until flush(), which is what makes the ordering a property of the
    ladder above rather than of the order check_game() happens to call things.

    Two behaviours beyond sorting:
      * merge_into -- routine traffic can fold itself into a consolidated push
        going out on the same poll instead of being a second push. It names
        candidate targets in preference order and folds into the first one
        actually in the batch; if none is (not an inning boundary, or the
        target raised), the alert emits on its own. Folding can never silently
        lose an alert.
      * per-alert isolation on send -- one push failing must not drop the ones
        queued behind it, least of all the golden one at the top.
    """

    def __init__(self):
        self._pending = []

    def add(self, kind, title, message, priority="default", tags="baseball",
            merge_into=None, merge_line=None):
        rank = ALERT_RANK.get(kind)
        if rank is None:
            print(
                f"[bot] Alert kind '{kind}' is not in ALERT_RANK -- sending it as "
                f"lowest importance. Add it to the ladder."
            )
            rank = UNRANKED_ALERT_RANK
        self._pending.append(
            {
                "kind": kind,
                "rank": rank,
                "title": title,
                "message": message,
                "priority": priority,
                "tags": tags,
                "merge_into": merge_into,
                "merge_line": merge_line,
            }
        )

    def has(self, kind):
        return any(a["kind"] == kind for a in self._pending)

    def kinds(self):
        return [a["kind"] for a in self._pending]

    def _fold(self):
        """Fold merge_into alerts into the first candidate target present."""
        by_kind = {a["kind"]: a for a in self._pending}
        kept = []
        for alert in self._pending:
            wanted = alert.get("merge_into") or ()
            if isinstance(wanted, str):
                wanted = (wanted,)
            target = next(
                (by_kind[k] for k in wanted if k in by_kind and by_kind[k] is not alert),
                None,
            )
            if target is None:
                kept.append(alert)
                continue
            line = alert.get("merge_line") or alert["message"]
            target["message"] = f"{target['message']}\n\n{line}"
        self._pending = kept

    def flush(self):
        """Send the batch, least important first. Returns the kinds actually
        sent, in send order.

        During the first poll after the process starts, every non-golden alert
        is computed and marked as alerted but NOT sent -- see _seeding_poll().
        """
        self._fold()
        # Stable sort: same-rank alerts keep the order the tiers computed them
        # in (several golden hits in one poll stay chronological).
        ordered = sorted(self._pending, key=lambda a: a["rank"])
        self._pending = []
        if _seeding:
            # First poll after a restart. Every condition already true when we
            # booted (a lead that crossed hours ago, a bullpen already three
            # deep) would otherwise re-fire as though it just happened, which
            # is what made every redeploy replay the whole slate. The tiers
            # have already recorded their keys in `alerted`, so dropping the
            # sends here adopts the current state silently.
            #
            # GOLDEN IS EXEMPT. A position player on the mound at boot is the
            # one thing worth a push even if it started before we did.
            held = [a["kind"] for a in ordered if a["kind"] != "golden"]
            if held:
                print(f"[bot] Startup: adopted {len(held)} in-progress "
                      f"condition(s) without alerting: {', '.join(held)}")
            ordered = [a for a in ordered if a["kind"] == "golden"]
        sent = []
        for alert in ordered:
            try:
                notifier.send_alert(
                    alert["title"], alert["message"],
                    priority=alert["priority"], tags=alert["tags"],
                )
                sent.append(alert["kind"])
            except Exception as e:
                print(f"[bot] Push failed for '{alert['kind']}' (continuing): {e}")
        return sent


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


def check_urgency_mode(game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs, alerted, bus):
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
            _last_digest_lead.pop(game_pk, None)
            bus.add(
                "urgency_off",
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
            _last_digest_lead.pop(game_pk, None)
            bus.add(
                "urgency_off",
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
    team_id = linescore.get(f"{trailing}_id")
    parts.extend(_arm_intel_lines(boxscore, trailing, team_id))
    left_line = _remaining_line(boxscore, trailing, team_id)
    if left_line:
        parts.append(left_line)
    fatigue = _pen_fatigue_line(boxscore, trailing, team_id)
    if fatigue:
        parts.append(fatigue)
    parts.append("")
    parts.append("Now reporting EVERY half-inning and EVERY pitcher change in this game.")

    bus.add(
        "urgency_on",
        "Urgency Mode ON",
        "\n".join(parts),
        priority="urgent",
        tags="rotating_light,stopwatch",
    )
    _last_digest_lead[game_pk] = lead
    return True


def consume_inning_change(game_pk, inning, inning_state):
    """Advance the half-inning baseline and report whether it just flipped.

    Split out of the old check_inning_change so the baseline advances on EVERY
    poll, before anything decides whether to push. That is the existing
    "advance the baseline even when suppressing" discipline, made unconditional:
    an inning boundary that is folded, suppressed or gated can never come back
    on the next poll.

    Only Top and Bottom count. MLB also reports "Middle"/"End" between halves,
    which would double the notification volume without adding information.
    """
    if inning is None or inning_state not in ("Top", "Bottom"):
        return False

    marker = (inning, inning_state)
    baseline = _last_half_inning.get(game_pk)
    _last_half_inning[game_pk] = marker

    # Unchanged, or first sighting after a restart -- re-seed without firing.
    if baseline is None or baseline == marker:
        return False
    return True


def check_inning_report(game_pk, boxscore, linescore, inning, inning_ordinal,
                        inning_state, outs, alerted, bus, changed, suppress=False):
    """The routine per-inning push, consolidated into ONE notification.

    An inning boundary used to produce an inning-change ping and, on the same
    poll, whatever lead ping the scoreboard happened to trip -- separate pushes
    competing for the top of the stack. This is now a single digest carrying
    the three things worth knowing at an inning boundary: the inning, the
    current lead (and how far it moved since the last report), and the trailing
    team's bullpen state -- relievers used, who is on the mound, pitch count.

    This is the quietest push the bot sends (ntfy "low"). Nothing folds into
    it any more: Big Lead and Extreme Lead are things you would look up from
    the game for, and folding them here would demote them to this one's
    priority.

    Unchanged from before: urgency-mode only, once per Top/Bottom transition.
    """
    if not changed:
        return
    if not _in_urgency(game_pk, alerted):
        return
    # Suppressed only by urgency ENTRY, whose message already is a full
    # report and which fires once per game. A bullpen rung does NOT suppress
    # it. The baseline was already advanced by consume_inning_change(), so a
    # suppressed report costs no future one.
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

    previous = _last_digest_lead.get(game_pk)
    _last_digest_lead[game_pk] = lead
    if previous is not None and previous != lead:
        parts.append("")
        parts.append(f"Lead {lead - previous:+d} since the last report.")

    bus.add(
        "inning_digest",
        f"Inning Report - {inning_state} {inning_ordinal or inning}",
        "\n".join(parts),
        # Deliberately the quietest push the bot sends. It is context for a
        # game you are probably already watching, so it must never buzz harder
        # than Big Lead or Bullpen Exhausted, which are the ones worth looking
        # up for.
        priority="low",
        tags="stopwatch",
    )


def check_big_lead(game_pk, linescore, inning, inning_ordinal, inning_state, outs, alerted, bus):
    """Score-only watch tier, completely independent of who's pitching: fires
    the first time a game's run differential crosses BIG_LEAD_THRESHOLD, and
    again every BIG_LEAD_STEP runs after that as the lead keeps growing. Also
    marks the game as "big lead flagged" (via the alerted key itself) so
    check_pitcher_change() knows to start watching it.

    Mid-inning this is its own push, immediately -- a lead running away is the
    thing you want to hear about while it is happening. On a poll that is also
    an inning boundary it folds into the inning report instead, so the boundary
    produces one notification rather than two saying the same thing. The bucket
    key is recorded either way, so the fold cannot cost a future alert."""
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
    # The part that carries the news, kept separate from the score/inning
    # header: the inning report already prints the header, so folding sends
    # only this tail rather than repeating it.
    tail = f"Lead is up to {lead} runs. Worth watching for a bullpen move."

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
                    tail += (
                        f"\nCurrent {game_odds.get('book_title', 'book')} line: "
                        f"{leading_team} {point:+g} ({price:+d})"
                    )
    except Exception as e:
        print(f"[bot] Big-lead odds lookup failed for {game_pk}: {e}")

    message = (
        f"{leading_team} {leading_runs} - {trailing_team} {trailing_runs}\n"
        f"{inning_line}\n\n"
        f"{tail}"
    )
    bus.add(
        "big_lead", "Big Lead Watch", message, priority="default", tags="eyes",
        # Urgency entry and the inning report both already print the score,
        # inning and bullpen state, so on those polls this is a redundant
        # second push -- fold the news into whichever one is going out. Urgency
        # entry is preferred because it suppresses the inning report anyway.
        # Folds into Urgency Mode ON (which outranks it) but NOT into the
        # inning report -- folding there would demote a Big Lead into the
        # quietest push on the board.
        merge_into=("urgency_on",),
        merge_line=tail,
    )


def check_extreme_lead(game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs, alerted, bus):
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

    # Deliberately never folded into the inning report. Big Lead is a routine
    # watch line; this is a lead that has run away, and it has to stay its own
    # push, above the routine traffic, even on an inning boundary.
    bus.add(
        "extreme_lead",
        "Extreme Lead Alert",
        "\n".join(message_parts),
        priority="urgent",
        tags="rotating_light,fire",
    )


def check_bullpen_depth(game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs, alerted, bus):
    """TIER 1 -- the bullpen depth ladder for the trailing team.

    Fires on reliever count alone. There is no run-diff bar and no inning
    floor: a team four arms deep has told you something about the game no
    matter what the scoreboard says, and gating this on a lead would silence
    it in exactly the grind-it-out games where it is the only warning.

      BULLPEN_EXHAUSTION_COUNT relievers -> "Bullpen Exhausted", high
      BULLPEN_CRITICAL_COUNT   relievers -> same alert, urgent

    One push per rung per (game, side), persisted in `alerted` so a restart
    mid-game does not re-announce a rung already sent. Keyed by side so a lead
    change correctly starts a fresh ladder for the newly trailing team.

    Returns the rung that fired, so check_pitcher_change() can skip its own
    push for the same substitution.
    """
    trailing = _trailing_side(linescore)
    if trailing is None:
        return  # tied -- nobody is conceding anything yet

    relievers = _relievers_used(boxscore, trailing)
    if relievers >= BULLPEN_CRITICAL_COUNT:
        rung = BULLPEN_CRITICAL_COUNT
    elif relievers >= BULLPEN_EXHAUSTION_COUNT:
        rung = BULLPEN_EXHAUSTION_COUNT
    else:
        return

    key = f"bullpen:{game_pk}:{trailing}:{rung}"
    if key in alerted:
        return
    alerted.add(key)
    # A bot that comes up mid-game already past both rungs should announce the
    # rung the game is actually at, once -- not walk the ladder retroactively.
    alerted.add(f"bullpen:{game_pk}:{trailing}:{BULLPEN_EXHAUSTION_COUNT}")

    # If the arm that tripped this rung IS a position player, TIER 0 already
    # owns the moment and says everything this ping would. Stay quiet, but keep
    # the rung marked so it can't fire late.
    pen = mlb_api.get_bullpen_detail(boxscore, trailing)
    if pen and pen[-1].get("is_position_player"):
        return

    leading = "home" if trailing == "away" else "away"
    leading_team = linescore[f"{leading}_name"]
    trailing_team = linescore[f"{trailing}_name"]
    lead = linescore[leading] - linescore[trailing]

    if rung >= BULLPEN_CRITICAL_COUNT:
        kind = "bullpen_critical"
        title = "Bullpen Exhausted - URGENT"
        priority = "urgent"
        tags = "rotating_light,fire"
        closer = (
            f"{trailing_team}: {relievers} relievers deep. This is the state a "
            f"position player usually comes out of -- watch this game."
        )
    else:
        kind = "bullpen_exhausted"
        title = "Bullpen Exhausted"
        priority = "high"
        tags = "warning,baseball"
        closer = (
            f"{trailing_team}: {relievers} relievers deep and running short of arms. "
            f"Escalates to urgent at {BULLPEN_CRITICAL_COUNT}."
        )

    parts = [
        f"{leading_team} {linescore[leading]} - {trailing_team} {linescore[trailing]}",
        _format_inning_line(inning, inning_ordinal, inning_state, outs),
        "",
    ]
    pen_line = _bullpen_state_line(boxscore, trailing)
    if pen_line:
        parts.append(pen_line)

    # What just walked in, how tired he is, and what is left behind him. This
    # is the rung that leads into Tier 0, so it is the push that most needs to
    # say whether the lead is likely to keep growing.
    team_id = linescore.get(f"{trailing}_id")
    parts.extend(_arm_intel_lines(boxscore, trailing, team_id))
    left_line = _remaining_line(boxscore, trailing, team_id)
    if left_line:
        parts.append(left_line)
    fatigue_line = _pen_fatigue_line(boxscore, trailing, team_id)
    if fatigue_line:
        parts.append(fatigue_line)

    parts.append("")
    parts.append(closer)
    if lead > 0:
        parts.append(f"{leading_team} lead: {lead}.")

    bus.add(kind, title, "\n".join(parts), priority=priority, tags=tags)
    return rung


def check_pitcher_change(game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs, alerted, bus, suppress=False):
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
    # Roster position, not boxscore position -- the boxscore relabels a
    # position player as "P" the moment they take the mound, which is exactly
    # the case this guard exists for.
    if mlb_api.is_position_player(
        new_pitcher.get("person", {}).get("id"),
        fallback_abbr=new_pitcher.get("position", {}).get("abbreviation"),
    ):
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

    lines = [
        f"{header}{leading_team} {linescore[leading_side]} - "
        f"{trailing_team} {linescore[trailing_side]}",
        inning_line,
        "",
        f"{pitcher_name} is in for {trailing_team} (reliever #{current_count}).",
    ]
    # What actually walked in, and how tired he and the pen behind him are. A
    # change only tells you the bet if you know whether the new arm is worse
    # than the one that left, and whether anyone decent is still behind him.
    team_id = linescore.get(f"{trailing_side}_id")
    lines.extend(_arm_intel_lines(boxscore, trailing_side, team_id))
    left = _remaining_line(boxscore, trailing_side, team_id)
    if left:
        lines.append(left)
    fatigue = _pen_fatigue_line(boxscore, trailing_side, team_id)
    if fatigue:
        lines.append(fatigue)
    lines += ["", footer]
    bus.add("pitcher_change", title, "\n".join(lines), priority="urgent", tags=tags)


def required_run_diff_for_inning(inning):
    """Run differential needed to trigger the ordinary 'blowout' tier at a
    given inning. An unknown inning uses the early-game bar -- the same value
    innings 1-6 use, so the two can never silently disagree."""
    if inning is None:
        return RUN_DIFF_MID
    if inning <= 6:
        return RUN_DIFF_MID
    return RUN_DIFF_LATE


def _classify(hit, boxscore, linescore, inning):
    """Decide which tier a position-player-pitching event fires at.

    A position player on the mound ALWAYS signals -- this never returns None.
    The tiers only grade how strong the betting case is:

      bullpen_exhausted  the losing team burned BULLPEN_EXHAUSTION_COUNT+
                         relievers first -- the same bar the standalone
                         bullpen ladder uses. Strongest form of the concession
                         signal; bypasses the inning/run-diff gate entirely.
      blowout            run differential clears the bar for the inning. There
                         is no inning floor -- the bar itself just drops later
                         in the game.
      position_player    catch-all. The run-diff bar did not clear, but a field
                         player is pitching and that is the event this bot
                         exists to see. Missing it because the differential was
                         one short would be worse than an extra push.

    Returns (tier, run_diff, prior_relievers).
    """
    conceding_side = hit["side"]
    bet_side = "home" if conceding_side == "away" else "away"

    conceding_runs = linescore[conceding_side]
    bet_runs = linescore[bet_side]
    run_diff = bet_runs - conceding_runs

    # Pitchers list includes whoever is pitching right now (the position
    # player) -- subtract them to get relievers actually burned beforehand.
    prior_relievers = mlb_api.count_pitchers_used(boxscore, conceding_side) - 1

    if run_diff > 0:
        if prior_relievers >= BULLPEN_EXHAUSTION_COUNT:
            return "bullpen_exhausted", run_diff, prior_relievers
        # No inning floor. The bet is the winning side's spread WIDENING, and a
        # concession in the 3rd leaves more innings for that to happen than one
        # in the 8th -- an early inning is if anything a point in the bet's
        # favour, so it must never downgrade the tier.
        if run_diff >= required_run_diff_for_inning(inning):
            return "blowout", run_diff, prior_relievers

    return "position_player", run_diff, prior_relievers


def _pen_intel_worth_it(box, detail, urgency):
    """Whether a game's pen is in a state worth calling out.

    No longer gates the snapshot -- `bullpen.compact()` cut the per-team
    payload far enough that every live game can carry it, which is what makes
    the data visible on any game you happen to open rather than only the loud
    ones. Kept because "is the thesis live here" is a useful predicate.
    """
    if urgency:
        return True
    lead = abs((detail.get("home_runs") or 0) - (detail.get("away_runs") or 0))
    if lead >= RUN_DIFF_LATE:
        return True
    for side in ("away", "home"):
        used = len(box["teams"][side].get("pitchers", []))
        if used - 1 >= URGENCY_MIN_RELIEVERS:
            return True
    return False


def _arm_intel_lines(boxscore, side, team_id):
    """What the arm that just took the mound actually is, and how tired.

    A pitching change is only worth a push if you know what walked in. Season
    grade says whether he is attackable; workload says whether he is on fumes.
    Both come from the day-cached roster pull, so this adds no request.
    """
    if not team_id:
        return []
    pen = _safe("intel/pen", mlb_api.get_bullpen_detail, boxscore, side) or []
    if not pen:
        return []
    current = pen[-1]
    arm = _safe("intel/arm", bullpen.get_arm, team_id, current.get("player_id"))
    if not arm:
        return []

    lines = []
    stats = []
    if arm.get("era") is not None:
        stats.append(f"{arm['era']} ERA")
    if arm.get("k_bb_pct") is not None:
        stats.append(f"{arm['k_bb_pct']}% K-BB")
    if arm.get("whip") is not None:
        stats.append(f"{arm['whip']} WHIP")
    verdict = arm.get("verdict")
    if stats or verdict:
        head = f"{current.get('name', 'New arm')}: " + " - ".join(stats)
        if verdict:
            head += f"  [{verdict}]"
        lines.append(head)

    w = arm.get("workload") or {}
    if w.get("availability") and w["availability"] != "UNKNOWN":
        lines.append(f"Fatigue: {w['availability']} ({w.get('why')})"
                     + (f", {w['appearances_7d']} app in 7d"
                        if w.get("appearances_7d") else ""))
    return lines


def _pen_fatigue_line(boxscore, side, team_id):
    """How hard the whole pen has been worked lately. A pen can arrive at the
    park already exhausted, which the in-game reliever count cannot show."""
    if not team_id:
        return None
    r = _safe("pen_fatigue", bullpen.get_remaining, boxscore, side, team_id)
    if not r:
        return None
    bits = []
    if r.get("pen_pitches_2d"):
        bits.append(f"{r['pen_pitches_2d']} pitches in 2 days")
    if r.get("gassed_count"):
        bits.append(f"{r['gassed_count']} gassed")
    if r.get("limited_count"):
        bits.append(f"{r['limited_count']} limited")
    if not bits:
        return None
    return "Pen fatigue: " + " - ".join(bits)


def _remaining_line(boxscore, side, team_id):
    """One line on what the trailing team still has to run out there.

    This is the read the alert was missing: how many relievers have gone tells
    you where the game has been, what is LEFT tells you whether the lead keeps
    growing -- which is the actual bet. Never raises; an unavailable roster
    means "unknown", never "nobody left".
    """
    if not team_id:
        return None
    r = _safe("remaining_pen", bullpen.get_remaining, boxscore, side, team_id)
    if not r or not r.get("count"):
        return None
    bits = [f"{r['count']} arm(s) left", r["verdict"]]
    if r.get("combined_era") is not None:
        bits.append(f"{r['combined_era']} combined ERA")
    if r.get("attack_count"):
        bits.append(f"{r['attack_count']} attackable")
    return "Pen left: " + " - ".join(bits)


def _safe(label, fn, *args, **kwargs):
    """Run one tier in isolation. A tier that raises must never take the others
    down with it -- least of all the golden check."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"[bot] Tier '{label}' failed (continuing): {e}")
        return None


def check_game(game_pk, alerted):
    """One poll of one game. Returns the alert kinds pushed, in send order.

    Rule hierarchy, deliberately ordered:

      TIER 0  GOLDEN -- a position player is pitching. This is the event the
              bot exists for. It is COMPUTED FIRST, needs nothing but the
              boxscore, and is isolated so no other tier's failure can suppress
              it. It always fires (see _classify) and overrides every gate.

      TIER 1  CONTEXT -- bullpen depth, big lead, extreme lead, urgency mode,
              the inning report, pitcher change. These do not gate the golden
              signal; they exist to flag games where it is becoming likely, so
              the bettor is already watching when it happens. Each runs in
              isolation.

    Computation order and SEND order are two different things now, and only the
    first one is decided here. Every tier writes into an AlertBus; the bus
    sends the batch at the end, sorted by ALERT_RANK, least important first --
    so the most important push is the last one sent and therefore the one
    sitting on top of the phone's notification stack.

    That split is the point. Golden must be computed first (nothing that can
    fail may run before it) and sent last (nothing may cover it), and those two
    requirements are contradictory as long as a tier pushes the moment it
    decides to. Adding a tier in the wrong place here can no longer bury
    anything; it can only change what gets computed first.

    The flush is in a `finally` so a tier that escapes _safe() still cannot
    swallow a golden push already sitting in the batch.
    """
    boxscore = mlb_api.get_boxscore(game_pk)   # hard requirement for any check
    linescore = mlb_api.get_linescore(boxscore)

    # Inning context is best-effort. A linescore failure degrades the golden
    # alert's wording (inning unknown) but must never cost us the alert.
    detail = _safe("linescore_detail", mlb_api.get_linescore_detail, game_pk) or {}
    inning = detail.get("inning")
    inning_ordinal = detail.get("inning_ordinal")
    inning_state = detail.get("inning_state")
    outs = detail.get("outs")

    bus = AlertBus()
    try:
        # ---- TIER 0: GOLDEN, computed first and isolated ----
        _safe("golden/position_player", check_position_players,
              game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs,
              alerted, bus)

        # ---- TIER 1: context signals ----
        # Computation order still matters for the SUPPRESSION rules (which tier
        # gets to know what another tier already said); it no longer decides
        # what ends up on top of the stack.
        #
        # The half-inning marker is consumed up front, before any tier can
        # raise, so the baseline advances exactly once per poll no matter what
        # happens below.
        inning_changed = _safe("inning_marker", consume_inning_change,
                               game_pk, inning, inning_state)

        # Urgency latches first so the inning report and pitcher check see the
        # current mode. Extreme runs before big-lead because big-lead
        # self-suppresses inside extreme territory, keeping the two from
        # double-alerting on overlapping buckets.
        entered_urgency = _safe("urgency_mode", check_urgency_mode,
            game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs,
            alerted, bus)
        bullpen_rung = _safe("bullpen_depth", check_bullpen_depth,
            game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs,
            alerted, bus)
        _safe("extreme_lead", check_extreme_lead,
            game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs,
            alerted, bus)
        _safe("big_lead", check_big_lead,
            game_pk, linescore, inning, inning_ordinal, inning_state, outs, alerted, bus)
        # A bullpen rung no longer suppresses this: both go out, and because
        # the rung outranks the report it is sent last and sits on top, with
        # the inning report directly beneath it. Urgency ENTRY still suppresses
        # -- that message already is a full report, and it fires once.
        _safe("inning_report", check_inning_report,
            game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs,
            alerted, bus,
            changed=bool(inning_changed),
            suppress=bool(entered_urgency))
        # Both urgency entry and a bullpen rung already name the incoming
        # pitcher, so the generic pitcher-change ping would be a second push
        # for one move.
        _safe("pitcher_change", check_pitcher_change,
            game_pk, boxscore, linescore, inning, inning_ordinal, inning_state, outs,
            alerted, bus,
            suppress=bool(entered_urgency) or bool(bullpen_rung))
    finally:
        sent = bus.flush()
    return sent


def check_position_players(game_pk, boxscore, linescore, inning, inning_ordinal,
                           inning_state, outs, alerted, bus):
    """TIER 0 -- the golden signal. A field player on the mound overrides every
    other rule and always pushes; _classify only grades how strong the betting
    case is. Deduped per (game, player) so one appearance pushes once.

    This is computed before any other tier and never merges into anything. It
    queues at the top of the ALERT_RANK ladder, which means it is the LAST push
    sent and therefore the one on top of the stack -- not the first one sent
    and buried under six context pings, which is what it used to be."""
    hits = mlb_api.find_position_players_pitching(boxscore)
    if not hits:
        return

    for hit in hits:
        key = f"{game_pk}:{hit['player_id']}"
        if key in alerted:
            continue

        # _classify never returns None -- a field player on the mound always
        # signals, the tier only grades how strong the betting case is.
        tier, run_diff, prior_relievers = _classify(hit, boxscore, linescore, inning)

        conceding_side = hit["side"]
        bet_side = "home" if conceding_side == "away" else "away"
        bet_team = linescore[f"{bet_side}_name"]
        conceding_team = linescore[f"{conceding_side}_name"]
        bet_runs = linescore[bet_side]
        conceding_runs = linescore[conceding_side]

        if tier == "bullpen_exhausted":
            title = "GOLDEN - Position Player (Bullpen Exhausted)"
            priority = "urgent"
            tags = "rotating_light,fire"
        elif tier == "blowout":
            title = "GOLDEN - Position Player (Blowout)"
            priority = "high"
            tags = "baseball,rotating_light"
        else:
            # Catch-all. Still urgent on the phone -- a field player on the
            # mound is the whole point of the bot, and deciding it is not worth
            # a look is the bettor's call, not the bot's.
            title = "GOLDEN - Position Player Pitching"
            priority = "urgent"
            tags = "rotating_light,baseball"

        inning_line = _format_inning_line(inning, inning_ordinal, inning_state, outs)

        losing = run_diff > 0
        description = f"{hit['name']} ({hit['real_position']}) is pitching for {conceding_team}. "
        if losing:
            description += (
                f"{conceding_team} has conceded -- consider betting {bet_team}'s spread to "
                f"increase (cover a bigger number) from here."
            )
        else:
            # Tied or ahead: not a concession, so do not dress it up as a bet.
            description += (
                f"{conceding_team} is not trailing, so this is not the usual concession "
                f"signal -- most likely an extra-innings or emergency situation. Worth a look, "
                f"not an automatic bet."
            )

        if tier == "position_player" and losing:
            # Only the run differential can hold a tier back now -- say so, so
            # the push explains its own confidence. Inning is never a reason.
            needed = required_run_diff_for_inning(inning)
            if run_diff < needed:
                description += (
                    f" Note: {run_diff}-run lead is short of the {needed} this inning "
                    f"normally needs, and only {prior_relievers} reliever(s) were used."
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
        # Odds are a nice-to-have on the golden alert. If the book is down or
        # out of quota we still push -- the line can be looked up by hand, a
        # missed concession cannot be recovered.
        odds_snapshot = _safe("golden/odds", odds.fetch_for_alert, home_team, away_team)

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
        bus.add("golden", title, message, priority=priority, tags=tags)
        # Marked alerted as soon as the push is queued. The queue is flushed in
        # a `finally` inside check_game, so "queued" and "sent" cannot come
        # apart -- and notifier.send_alert swallows every failure anyway, so
        # this is the same guarantee as marking it after the send used to be:
        # one appearance pushes once, never twice.
        alerted.add(key)
        _safe("golden/alert_log", alert_log.append,
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


def _hot_reasons(g):
    """Why this live game deserves the faster poll, or [] if it doesn't.

    Every condition reuses a threshold that already exists elsewhere in the
    rulebook -- no new numbers to keep in sync. These are the states where a
    position player is plausibly the next arm."""
    if not g.get("is_live"):
        return []

    away, home = g.get("away_runs") or 0, g.get("home_runs") or 0
    lead = abs(home - away)
    inning = g.get("inning")
    reasons = []

    if g.get("urgency"):
        reasons.append("urgency mode")

    if lead >= EXTREME_LEAD_THRESHOLD:
        reasons.append(f"extreme lead ({lead})")
    elif lead >= BIG_LEAD_THRESHOLD:
        reasons.append(f"big lead ({lead})")

    if away != home:
        trailing = "away" if home > away else "home"
        relievers = max(0, len(g.get(f"{trailing}_bullpen") or []) - 1)
        if relievers >= BULLPEN_CRITICAL_COUNT:
            reasons.append(f"bullpen critical ({relievers} used)")
        elif relievers >= BULLPEN_EXHAUSTION_COUNT:
            reasons.append(f"bullpen exhausted ({relievers} used)")

    # Late and already past the blowout bar -- the classic concession window.
    if inning is not None and inning >= 7 and lead >= RUN_DIFF_LATE:
        reasons.append(f"late innings (inning {inning}, lead {lead})")

    return reasons


def _poll_interval_for(payload):
    """Pick this cycle's sleep. Fast while ANY live game is hot; back to normal
    as soon as none is -- which is what makes it revert when the game ends."""
    global _fast_poll_active

    hot = []
    for g in (payload or {}).get("games", []):
        why = _hot_reasons(g)
        if why:
            hot.append((f"{g.get('away_team')} @ {g.get('home_team')}", why))

    if hot and not _fast_poll_active:
        _fast_poll_active = True
        print(f"[bot] FAST POLL on ({FAST_POLL_INTERVAL_SECONDS}s):")
        for matchup, why in hot:
            print(f"[bot]   {matchup} -- {', '.join(why)}")
    elif not hot and _fast_poll_active:
        _fast_poll_active = False
        print(f"[bot] No hot games. Back to {POLL_INTERVAL_SECONDS}s polling.")

    return FAST_POLL_INTERVAL_SECONDS if hot else POLL_INTERVAL_SECONDS


def _write_status(live_game_count):
    import json

    with open(paths.data_path("status.json"), "w") as f:
        json.dump(
            {
                "last_checked": _now_iso(),
                "live_games": live_game_count,
                "bot_state": _bot_state,
                "build": BUILD_ID,
                "schedule": _schedule_block(),
                "run_diff_mid": RUN_DIFF_MID,
                "run_diff_late": RUN_DIFF_LATE,
                "bullpen_exhaustion_count": BULLPEN_EXHAUSTION_COUNT,
                "bullpen_critical_count": BULLPEN_CRITICAL_COUNT,
                "big_lead_threshold": BIG_LEAD_THRESHOLD,
                "big_lead_step": BIG_LEAD_STEP,
                "extreme_lead_threshold": EXTREME_LEAD_THRESHOLD,
                "extreme_lead_step": EXTREME_LEAD_STEP,
                "urgency_run_diff": URGENCY_RUN_DIFF,
                "urgency_min_relievers": URGENCY_MIN_RELIEVERS,
                "urgency_exit_run_diff": URGENCY_EXIT_RUN_DIFF,
                "poll_interval_seconds": (
                    FAST_POLL_INTERVAL_SECONDS if _fast_poll_active else POLL_INTERVAL_SECONDS
                ),
                "base_poll_interval_seconds": POLL_INTERVAL_SECONDS,
                "fast_poll_interval_seconds": FAST_POLL_INTERVAL_SECONDS,
                "fast_poll_active": _fast_poll_active,
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
                # Every key below is present on Preview/Final games too (as
                # None/[]/{}) so anything rendering this feed can rely on one
                # stable shape and never has to test for missing keys.
                "away_hits": None, "home_hits": None,
                "away_errors": None, "home_errors": None,
                "away_lob": None, "home_lob": None,
                "is_top_inning": None, "scheduled_innings": None,
                "batting_side": None, "fielding_side": None,
                "batter": None, "on_deck": None, "current_pitcher": None,
                "innings": [], "defense": {},
                "away_remaining": None, "home_remaining": None,
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
                    # All of the following ride along in the linescore response
                    # already fetched above -- no extra request.
                    "away_hits": detail.get("away_hits"),
                    "home_hits": detail.get("home_hits"),
                    "away_errors": detail.get("away_errors"),
                    "home_errors": detail.get("home_errors"),
                    "away_lob": detail.get("away_lob"),
                    "home_lob": detail.get("home_lob"),
                    "is_top_inning": detail.get("is_top_inning"),
                    "scheduled_innings": detail.get("scheduled_innings"),
                    "batting_side": detail.get("batting_side"),
                    "fielding_side": detail.get("fielding_side"),
                    "batter": detail.get("batter"),
                    "on_deck": detail.get("on_deck"),
                    "current_pitcher": detail.get("current_pitcher"),
                    "innings": detail.get("innings") or [],
                    # What each side still has in the pen, graded. Rosters
                    # and season lines are cached for the day, so this adds no
                    # per-poll requests -- but it is bulky, so it rides along
                    # only where the thesis is live. See _pen_intel_worth_it.
                    "away_remaining": bullpen.compact(_safe(
                        "remaining/away", bullpen.get_remaining,
                        box, "away", entry.get("away_id"))),
                    "home_remaining": bullpen.compact(_safe(
                        "remaining/home", bullpen.get_remaining,
                        box, "home", entry.get("home_id"))),
                    # Position players currently on the field -- the pool the
                    # trailing team would pull from to send someone to pitch.
                    "defense": detail.get("defense") or {},
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


# Games essentially never run past this ET hour, so the carryover lookup is
# skipped for most of the day rather than costing a request every poll.
CARRYOVER_UNTIL_HOUR_ET = 6


def _carryover_live_pks(today_str):
    """Games from the PREVIOUS Eastern day that are still being played.

    MLB assigns a game to a slate by officialDate, so a 9:45pm ET first pitch
    belongs to THAT day -- but it is still in the 8th inning after midnight,
    by which time today_et() has rolled to the next date. Asking only for
    "today" drops the game, the scheduler sees tomorrow's slate hours away,
    and it goes to sleep mid-game.

    That is not hypothetical: on 2026-08-27 the bot went to sleep at 11:00pm CT
    with Arizona four relievers deep in a 1-6 game, and neither bullpen rung
    was ever sent.
    """
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo

    if datetime.now(ZoneInfo("America/New_York")).hour >= CARRYOVER_UNTIL_HOUR_ET:
        return []
    try:
        prev = (date.fromisoformat(today_str) - timedelta(days=1)).isoformat()
        return mlb_api.get_live_game_pks(prev)
    except Exception as e:
        print(f"[bot] Carryover lookup failed (continuing): {e}")
        return []


def run_once(alerted):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # MLB game dates are in ET. Using the server's local date breaks when the
    # process runs in UTC (e.g. Railway) between midnight ET and 3am ET —
    # date.today() rolls over to tomorrow while west-coast games are still live.
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    game_pks = mlb_api.get_live_game_pks(today)
    # A game that started before midnight ET is on yesterday's slate by
    # officialDate but is still being played. Keep watching it.
    for pk in _carryover_live_pks(today):
        if pk not in game_pks:
            game_pks.append(pk)
    alerts_this_cycle = []
    for game_pk in game_pks:
        try:
            alerts_this_cycle.extend(check_game(game_pk, alerted) or [])
        except Exception as e:
            print(f"[bot] Error checking game {game_pk}: {e}")
    _write_status(len(game_pks))
    snapshot_payload = _write_live_snapshot(game_pks, alerted)
    # The first poll only adopted whatever was already in progress; from here
    # on alerts send normally.
    _seeding_poll_done()
    return snapshot_payload


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
            payload = run_once(alerted)
            state.save_alerted(alerted)
            time.sleep(_poll_interval_for(payload))
            continue

        _current_window = window
        now = _utcnow()
        first = window.get("first_pitch_utc")
        backstop = window.get("backstop_utc")

        # Slate is over (or empty) -- sleep to the next day that has games.
        if not window.get("game_count") or window.get("all_final"):
            # Same carryover guard: "today has no games" is not a reason to
            # stop watching a game from last night that is still going.
            if _carryover_live_pks(today):
                _bot_state = "scanning"
                payload = None
                try:
                    payload = run_once(alerted)
                    state.save_alerted(alerted)
                except Exception as e:
                    print(f"[bot] Poll error: {e}")
                time.sleep(_poll_interval_for(payload))
                continue
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
            payload = None
            try:
                payload = run_once(alerted)
                state.save_alerted(alerted)
            except Exception as e:
                print(f"[bot] Poll error: {e}")
            time.sleep(_poll_interval_for(payload))
            continue

        # Games today but first pitch hasn't landed yet.
        if first:
            wake_at = first - timedelta(seconds=PREGAME_LEAD_SECONDS)
            if now < wake_at:
                # ...unless last night's slate is still being played. At
                # midnight ET `today` rolls forward while west-coast games are
                # in the 7th, and sleeping here abandons them mid-game.
                if _carryover_live_pks(today):
                    _bot_state = "scanning"
                    payload = None
                    try:
                        payload = run_once(alerted)
                        state.save_alerted(alerted)
                    except Exception as e:
                        print(f"[bot] Poll error: {e}")
                    time.sleep(_poll_interval_for(payload))
                    continue
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

        # Inside the window: normal polling. The roster check is an in-memory
        # timestamp comparison except at most once a day, when it does one
        # request -- keeps September call-ups on record without a restart.
        _ensure_roster()
        _bot_state = "scanning"
        payload = None
        try:
            payload = run_once(alerted)
            state.save_alerted(alerted)
        except Exception as e:
            print(f"[bot] Poll error: {e}")
        time.sleep(_poll_interval_for(payload))


def run_unscheduled(alerted):
    """Legacy 24/7 loop, used when SCHEDULE_ENABLED is off or schedule.py is
    unavailable."""
    while True:
        payload = None
        try:
            payload = run_once(alerted)
            state.save_alerted(alerted)
        except Exception as e:
            print(f"[bot] Poll error: {e}")
        time.sleep(_poll_interval_for(payload))


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


def _ensure_roster(max_age_hours=24):
    """Keep the league-wide primary-position record fresh. The record is what
    the position-player detector runs against; a daily sweep picks up call-ups
    and trades (September roster expansion especially). Non-fatal on failure:
    the lazy per-player lookup in mlb_api still covers anyone missing."""
    from datetime import datetime, timezone

    last = mlb_api.positions_refreshed_at()
    if last:
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
            if age_h < max_age_hours:
                return
        except ValueError:
            pass
    mlb_api.refresh_league_positions()


def run_loop(alerted):
    """Entrypoint shared by bot.py standalone and app.py's background thread."""
    _ensure_roster()
    if SCHEDULE_ENABLED and schedule is not None:
        _ensure_season_schedule()
        run_scheduled(alerted)
    else:
        why = "SCHEDULE_ENABLED=false" if schedule is not None else "schedule.py unavailable"
        print(f"[bot] Game-window scheduling off ({why}); polling 24/7.")
        run_unscheduled(alerted)


def main():
    print(
        f"MLB Blowout Bot starting. Blowout tier: "
        f"{RUN_DIFF_MID}+ runs (innings 1-6) / {RUN_DIFF_LATE}+ runs (7+), any inning. "
        f"Bullpen Exhausted: {BULLPEN_EXHAUSTION_COUNT} relievers (high) / "
        f"{BULLPEN_CRITICAL_COUNT} (urgent), any inning. "
        f"Polling every {POLL_INTERVAL_SECONDS}s."
    )
    alerted = state.load_alerted()
    try:
        run_loop(alerted)
    except KeyboardInterrupt:
        print("\n[bot] Stopped.")
        state.save_alerted(alerted)


if __name__ == "__main__":
    main()
