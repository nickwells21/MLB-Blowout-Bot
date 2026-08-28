"""Who is LEFT in a team's bullpen, and how attackable those arms are.

The dashboard has always shown which pitchers a team has *used*. What it could
not show is what is still in there -- and that is the thing that actually
decides the bet. The wager is the leading team's run line widening, which
depends less on the concession itself than on the quality of the arms the
trailing team still has to run out there.

A position player on the mound is the extreme endpoint of "we are out of arms
we trust". This module measures the rungs below that endpoint, so a soft pen
can be read before anyone concedes, while the line is still soft.

COST: two requests per team per day, both cached for the day --
  /teams/{id}/roster/active            who is on the active roster
  /people?personIds=...&hydrate=stats  every one of their season lines, ONE call
Rosters and season lines do not move during a game, so this never rides the
polling loop. It cannot slow down or interfere with golden-signal detection.

GRADING is taken from the screen Nick brought in, restricted to the metrics
this API actually serves. Stuff+, xFIP/SIERA, Barrel% and SwStr% are not
available without a scraped source and are deliberately NOT approximated --
a made-up Stuff+ would be worse than no Stuff+.
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

BASE = "https://statsapi.mlb.com/api/v1"
ET = ZoneInfo("America/New_York")

# team_id -> (fetched_epoch, payload). Rosters and season stats are stable
# within a day; a restart just refetches, which is two requests.
_cache = {}
CACHE_TTL_SECONDS = 6 * 3600

PITCHER_POSITIONS = ("P", "TWP")

# --- thresholds, straight from the screen ---------------------------------
# "Attack zone" is the column that matters: these are the arms you want the
# trailing team to be forced into.
ATTACK = {"k_pct": 18.0, "bb_pct": 10.0, "k_bb_pct": 10.0, "era": 4.50}
GOOD = {"k_pct": 25.0, "bb_pct": 7.0, "k_bb_pct": 18.0, "era": 3.50}

# Rate stats on a handful of batters are noise, and grading them is worse than
# useless -- an arm with six batters faced and a 0.00 ERA would otherwise come
# back ATTACK on a 16% walk rate. Below this, say the sample is thin instead of
# pretending to a verdict. Roughly 8-10 innings.
MIN_BATTERS_FACED = 30

# A starter on the active roster is not a bullpen option tonight. Counting them
# among "arms left" overstates the pen and softens the verdict exactly when it
# should be hardening.
STARTER_SHARE = 0.5

# --- recent-workload thresholds -------------------------------------------
# A pen can arrive at the ballpark already exhausted. These are the standard
# usage patterns a manager treats as unavailable or limited; they are
# heuristics, not truth -- we cannot see the training staff's list.
GASSED_CONSEC_DAYS = 3       # three days running: effectively unavailable
GASSED_PITCHES_YDAY = 45     # a heavy outing last night
GASSED_PITCHES_2D = 50       # or that much across the last two
LIMITED_PITCHES_2D = 30
RECENT_WINDOW_DAYS = 7


def _season(date_str=None):
    if date_str:
        try:
            return int(date_str[:4])
        except (TypeError, ValueError):
            pass
    return datetime.now(ET).year


def _get(url):
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _fetch_team_arms(team_id, season):
    """Every active pitcher on a team with their season line. Two requests."""
    roster = _get(f"{BASE}/teams/{team_id}/roster/active").get("roster", [])
    ids = [
        str(p["person"]["id"])
        for p in roster
        if (p.get("position") or {}).get("abbreviation") in PITCHER_POSITIONS
    ]
    if not ids:
        return {}
    people = _get(
        f"{BASE}/people?personIds={','.join(ids)}"
        f"&hydrate=stats(group=[pitching],type=[season,gameLog],season={season})"
    ).get("people", [])

    out = {}
    for person in people:
        season_splits, log_splits = [], []
        for block in person.get("stats") or []:
            kind = ((block.get("type") or {}).get("displayName") or "").lower()
            splits = block.get("splits") or []
            if kind == "gamelog":
                log_splits = splits
            elif kind == "season":
                season_splits = splits
        out[person["id"]] = {
            "player_id": person["id"],
            "name": person.get("fullName", "Unknown"),
            **_derive(_merge_season(season_splits)),
            "workload": _workload(log_splits),
            # Kept so get_arm() can recompute rest excluding today.
            "_log": log_splits,
        }
    return out


def _merge_season(splits):
    """A traded player gets one season split per team. Reading only the first
    would report half a season, so sum the counting stats and let the rate
    stats be recomputed from those totals."""
    if not splits:
        return {}
    if len(splits) == 1:
        return splits[0].get("stat") or {}
    total = {}
    for sp in splits:
        for k, v in (sp.get("stat") or {}).items():
            n = _num(v)
            if n is None:
                continue
            total[k] = total.get(k, 0) + n
    ip = total.get("outs")
    if ip:
        # ERA/WHIP are rates and cannot be summed; rebuild them from totals.
        innings = ip / 3.0
        if total.get("earnedRuns") is not None:
            total["era"] = round(9 * total["earnedRuns"] / innings, 2)
        walks_hits = (total.get("baseOnBalls", 0) + total.get("hits", 0))
        total["whip"] = round(walks_hits / innings, 2)
        total["inningsPitched"] = f"{int(ip // 3)}.{int(ip % 3)}"
    return total


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _derive(stat):
    """Season rate stats. Everything here comes from the one hydrated call."""
    bf = _num(stat.get("battersFaced")) or 0
    k = _num(stat.get("strikeOuts")) or 0
    bb = _num(stat.get("baseOnBalls")) or 0
    ip = stat.get("inningsPitched")

    k_pct = round(100 * k / bf, 1) if bf else None
    bb_pct = round(100 * bb / bf, 1) if bf else None
    gp = _num(stat.get("gamesPitched")) or 0
    gs = _num(stat.get("gamesStarted")) or 0
    return {
        "role": "SP" if gp and (gs / gp) >= STARTER_SHARE else "RP",
        "games_pitched": int(gp),
        "games_started": int(gs),
        "innings_pitched": ip,
        "era": stat.get("era"),
        "whip": stat.get("whip"),
        "hr9": stat.get("homeRunsPer9"),
        "k_pct": k_pct,
        "bb_pct": bb_pct,
        "k_bb_pct": (round(k_pct - bb_pct, 1)
                     if k_pct is not None and bb_pct is not None else None),
        "batters_faced": int(bf) if bf else 0,
        # No season line at all is usually a fresh call-up: an unproven arm in
        # a blowout is its own kind of signal, so say so rather than hiding it.
        "unproven": not bf,
        "thin_sample": bool(bf) and bf < MIN_BATTERS_FACED,
    }


def _workload(log_splits, today=None, exclude_today=False):
    """Recent usage from the game log -- the thing the active roster cannot
    tell you. An arm that threw 45 pitches last night is on the roster and is
    not getting up today.

    `exclude_today` reports the rest a pitcher had walking IN. For an arm who
    has just entered, today's own outing is already in the log, so including it
    makes every incoming pitcher read "already pitched today" -- true, useless,
    and it hides the fact he was on back-to-back days.
    """
    today = today or datetime.now(ET).date()
    days = {}
    for sp in log_splits:
        d = sp.get("date")
        if not d:
            continue
        try:
            day = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        if exclude_today and day >= today:
            continue
        pitches = _num((sp.get("stat") or {}).get("numberOfPitches")) or 0
        days[day] = days.get(day, 0) + pitches

    if not days:
        return {"availability": "UNKNOWN", "days_rest": None, "last_pitched": None,
                "pitches_yday": 0, "pitches_2d": 0, "appearances_7d": 0,
                "consecutive_days": 0, "why": "no game log"}

    last = max(days)
    rest = (today - last).days
    def on(offset):
        from datetime import timedelta
        return days.get(today - timedelta(days=offset), 0)

    y, d2 = on(1), on(1) + on(2)
    consec = 0
    for i in range(1, 5):
        if on(i) > 0:
            consec += 1
        else:
            break

    recent = sum(1 for d in days if 0 <= (today - d).days <= RECENT_WINDOW_DAYS)

    # Recency matters as much as volume: 25 pitches on each of the last two
    # nights is a spent arm, while one 52-pitch long-relief outing two days ago
    # is an arm most of the way back. Both total ~50 over two days, so a flat
    # two-day sum would call them the same thing. Only load that includes
    # YESTERDAY can reach GASSED.
    if consec >= GASSED_CONSEC_DAYS:
        avail, why = "GASSED", f"{consec} days running"
    elif y >= GASSED_PITCHES_YDAY:
        avail, why = "GASSED", f"{int(y)} pitches yesterday"
    elif y > 0 and d2 >= GASSED_PITCHES_2D:
        avail, why = "GASSED", f"{int(d2)} pitches in 2 days, worked yesterday"
    elif consec >= 2:
        avail, why = "LIMITED", "back-to-back days"
    elif d2 >= GASSED_PITCHES_2D:
        # A single heavy outing, a day removed. Still recovering, not spent.
        avail, why = "LIMITED", f"{int(d2)}-pitch outing {rest} days ago"
    elif d2 >= LIMITED_PITCHES_2D:
        avail, why = "LIMITED", f"{int(d2)} pitches in 2 days"
    elif rest == 0:
        avail, why = "LIMITED", "already pitched today"
    else:
        avail, why = "READY", f"{rest} days rest"

    return {
        "availability": avail,
        "days_rest": rest,
        "last_pitched": last.isoformat(),
        "pitches_yday": int(y),
        "pitches_2d": int(d2),
        "appearances_7d": recent,
        "consecutive_days": consec,
        "why": why,
    }


def grade(arm):
    """Score one arm against the screen. Returns (verdict, hits) where `hits`
    names the metrics that landed in the attack zone, so the verdict can always
    be audited rather than trusted."""
    if arm.get("unproven"):
        return "UNPROVEN", ["no season line"]
    if arm.get("thin_sample"):
        return "THIN", [f"only {arm.get('batters_faced', 0)} batters faced"]

    attack, good, hits = 0, 0, []

    def check(key, label, higher_is_worse):
        nonlocal attack, good
        v = _num(arm.get(key))
        if v is None:
            return
        bad_edge, good_edge = ATTACK[key], GOOD[key]
        if (v > bad_edge) if higher_is_worse else (v < bad_edge):
            attack += 1
            hits.append(f"{label} {v:g}")
        elif (v < good_edge) if higher_is_worse else (v > good_edge):
            good += 1

    check("k_bb_pct", "K-BB%", False)   # the screen's #2 metric, and my best one
    check("k_pct", "K%", False)
    check("bb_pct", "BB%", True)
    check("era", "ERA", True)

    net = attack - good
    if net >= 2:
        return "ATTACK", hits
    if net == 1:
        return "LEAN", hits
    if net <= -1:
        return "TOUGH", hits
    return "NEUTRAL", hits


def _team_arms(team_id, date_str=None):
    """Cached roster+stats for one team, or None if unavailable."""
    season = _season(date_str)
    key = (team_id, season)
    hit = _cache.get(key)
    if not hit or time.time() - hit[0] > CACHE_TTL_SECONDS:
        arms = _fetch_team_arms(team_id, season)
        if not arms:
            return None
        _cache[key] = (time.time(), arms)
        return arms
    return hit[1]


def _public(arm):
    """Strip internal keys before anything reaches the JSON feed. The raw game
    log is kept in cache to recompute rest, but it is many KB per pitcher and
    has no business in live_snapshot.json."""
    return {k: v for k, v in arm.items() if not k.startswith("_")}


def get_arm(team_id, person_id, date_str=None):
    """One graded arm WITH workload, by id -- including a pitcher already in
    the game. get_remaining() deliberately excludes anyone who has pitched, but
    when a change is flagged the arm that just entered is precisely the one
    worth reporting on. Returns None if unknown."""
    arms = _team_arms(team_id, date_str)
    if not arms:
        return None
    arm = arms.get(person_id)
    if not arm:
        return None
    verdict, hits = grade(arm)
    # Recompute workload ignoring today, so the number means "rest he had
    # coming in" rather than the tautology that he has pitched today.
    coming_in = _workload(arm.get("_log") or [], exclude_today=True)
    return {**_public(arm), "verdict": verdict, "why": hits, "workload": coming_in}


def compact(remaining):
    """Trim a get_remaining() result down to what the dashboard actually
    renders. The full object carries ~25 fields per arm; the UI shows eight.
    On a feed the phone refetches every 15 seconds that difference is the
    whole reason the data had to be rationed in the first place -- so ration
    the fields instead of the games, and it can ride along everywhere.

    Full detail stays available server-side via get_remaining()."""
    if not remaining:
        return remaining
    slim = []
    for a in remaining.get("arms", []):
        w = a.get("workload") or {}
        slim.append({
            "name": a.get("name"),
            "verdict": a.get("verdict"),
            "era": a.get("era"),
            "whip": a.get("whip"),
            "k_bb_pct": a.get("k_bb_pct"),
            "why": ", ".join(a.get("why") or []),
            "avail": w.get("availability"),
            "avail_why": w.get("why"),
            "days_rest": w.get("days_rest"),
            "pitches_2d": w.get("pitches_2d"),
        })
    return {
        "arms": slim,
        "count": remaining.get("count"),
        "verdict": remaining.get("verdict"),
        "combined_era": remaining.get("combined_era"),
        "avg_k_bb_pct": remaining.get("avg_k_bb_pct"),
        "attack_count": remaining.get("attack_count"),
        "gassed_count": remaining.get("gassed_count"),
        "limited_count": remaining.get("limited_count"),
        "ready_count": remaining.get("ready_count"),
        "pen_pitches_2d": remaining.get("pen_pitches_2d"),
        "starter_count": remaining.get("starter_count"),
    }


def get_remaining(boxscore, side, team_id, date_str=None):
    """Arms the team has NOT used yet this game, graded and summarised.

    Returns None if the roster could not be fetched -- callers must treat this
    as "unknown", never as "nobody left".
    """
    arms = _team_arms(team_id, date_str)
    if not arms:
        return None

    used = set(boxscore["teams"][side].get("pitchers", []))
    remaining, starters = [], []
    for pid, arm in arms.items():
        if pid in used:
            continue
        verdict, hits = grade(arm)
        entry = {**_public(arm), "verdict": verdict, "why": hits}
        # Starters are listed separately: available in extremis, but they are
        # not what the trailing team reaches for next, so they must not count
        # toward the pen verdict.
        (starters if arm.get("role") == "SP" else remaining).append(entry)

    # Worst first: the arms the trailing team is most likely to be forced into
    # are the ones that decide whether the lead keeps growing.
    rank = {"ATTACK": 0, "UNPROVEN": 1, "THIN": 2, "LEAN": 3, "NEUTRAL": 4, "TOUGH": 5}
    order = lambda a: (rank.get(a["verdict"], 9), -(_num(a.get("era")) or 0))
    remaining.sort(key=order)
    starters.sort(key=order)
    return {
        "arms": remaining,
        "count": len(remaining),
        "starters": starters,
        "starter_count": len(starters),
        "used": len(used),
        **_summarise(remaining),
    }


def _summarise(remaining):
    """Pen-level read. Combined ERA is innings-weighted, so a mop-up arm with
    four innings does not swing it the way a straight average would."""
    er, ip_total, kbb, attack, unproven = 0.0, 0.0, [], 0, 0
    gassed = limited = ready = pen_pitches_2d = 0
    for a in remaining:
        w = a.get("workload") or {}
        av = w.get("availability")
        if av == "GASSED":
            gassed += 1
        elif av == "LIMITED":
            limited += 1
        elif av == "READY":
            ready += 1
        pen_pitches_2d += w.get("pitches_2d") or 0
        if a["verdict"] == "ATTACK":
            attack += 1
        if a["verdict"] in ("UNPROVEN", "THIN"):
            unproven += 1
        # Thin samples are excluded from the aggregates too, for the same
        # reason they get no verdict.
        if a.get("thin_sample") or a.get("unproven"):
            continue
        if a.get("k_bb_pct") is not None:
            kbb.append(a["k_bb_pct"])
        era, ip = _num(a.get("era")), _ip_to_float(a.get("innings_pitched"))
        if era is not None and ip:
            er += era * ip / 9.0
            ip_total += ip
    return {
        "combined_era": round(9 * er / ip_total, 2) if ip_total else None,
        "avg_k_bb_pct": round(sum(kbb) / len(kbb), 1) if kbb else None,
        "attack_count": attack,
        "unproven_count": unproven,
        # Availability is separate from quality: a good arm that threw 45
        # pitches last night is not an option tonight either.
        "gassed_count": gassed,
        "limited_count": limited,
        "ready_count": ready,
        "pen_pitches_2d": int(pen_pitches_2d),
        "verdict": _pen_verdict(remaining, attack, unproven, gassed, ready),
    }


def _pen_verdict(remaining, attack, unproven, gassed=0, ready=None):
    """How soft is what is left.

    Counts EFFECTIVE arms, not bodies. A pen can arrive already exhausted from
    the previous two nights, and a roster spot held by an arm who threw 45
    pitches last night is not depth -- that is the whole point of tracking
    workload, so gassed arms are excluded from the count."""
    n = len(remaining)
    if n == 0:
        return "EMPTY"
    effective = n - gassed
    if effective <= 0:
        return "EMPTY"
    soft = attack + unproven
    if effective <= 2:
        return "NEARLY OUT"
    if soft >= max(2, effective * 0.6):
        return "SOFT"
    if soft == 0 and gassed == 0:
        return "INTACT"
    return "MIXED"


def _ip_to_float(ip):
    """MLB writes innings as '51.2' meaning 51 and 2/3. Averaging that string
    as a decimal quietly understates every workload, so convert properly."""
    if ip is None:
        return None
    try:
        whole, _, frac = str(ip).partition(".")
        return int(whole) + (int(frac[:1] or 0) / 3.0)
    except (TypeError, ValueError):
        return None
