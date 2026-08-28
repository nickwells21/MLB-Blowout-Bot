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
        f"&hydrate=stats(group=[pitching],type=[season],season={season})"
    ).get("people", [])

    out = {}
    for person in people:
        stat = {}
        for block in person.get("stats") or []:
            splits = block.get("splits") or []
            if splits:
                stat = splits[0].get("stat") or {}
                break
        out[person["id"]] = {
            "player_id": person["id"],
            "name": person.get("fullName", "Unknown"),
            **_derive(stat),
        }
    return out


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


def get_remaining(boxscore, side, team_id, date_str=None):
    """Arms the team has NOT used yet this game, graded and summarised.

    Returns None if the roster could not be fetched -- callers must treat this
    as "unknown", never as "nobody left".
    """
    season = _season(date_str)
    key = (team_id, season)
    hit = _cache.get(key)
    if not hit or time.time() - hit[0] > CACHE_TTL_SECONDS:
        arms = _fetch_team_arms(team_id, season)
        if not arms:
            return None
        _cache[key] = (time.time(), arms)
    else:
        arms = hit[1]

    used = set(boxscore["teams"][side].get("pitchers", []))
    remaining, starters = [], []
    for pid, arm in arms.items():
        if pid in used:
            continue
        verdict, hits = grade(arm)
        entry = {**arm, "verdict": verdict, "why": hits}
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
    for a in remaining:
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
        "verdict": _pen_verdict(remaining, attack, unproven),
    }


def _pen_verdict(remaining, attack, unproven):
    """How soft is what is left. Deliberately blunt -- this is a glance read."""
    n = len(remaining)
    if n == 0:
        return "EMPTY"
    soft = attack + unproven
    if n <= 2:
        return "NEARLY OUT"
    if soft >= max(2, n * 0.6):
        return "SOFT"
    if soft == 0:
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
