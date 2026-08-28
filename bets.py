"""The bet ledger: what was actually wagered, and whether it won.

This is the one thing the bot could not tell you before. It detects, it alerts,
it shows you the pen -- but nothing recorded whether the edge is real. Every
graded bet here is a data point on that question, and unlike the pen data it
genuinely does compound the longer this runs.

WHY THE LEDGER LIVES IN THE REPO, NOT IN DATA_DIR
On 2026-08-27 the first golden signal in production fired at 8:49pm CT and its
alerts_log entry was gone within the hour -- wiped by a redeploy, because
DATA_DIR is not a mounted Railway volume. A bet ledger written the same way
would lose your record every time the bot ships. So `bets.json` is committed:
it survives deploys, it is versioned, and losing it takes a git revert rather
than a container restart. The cost is that bets are added here rather than
from a phone. That trade is deliberate; a ledger you cannot trust is worse
than no ledger.

Grading is done against the final score from the MLB API, so a result is never
typed in by hand and cannot drift from what actually happened.
"""
import json
import os

import requests

BASE = "https://statsapi.mlb.com/api/v1"
LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bets.json")


def load():
    if not os.path.exists(LEDGER):
        return []
    with open(LEDGER, "r", encoding="utf-8") as f:
        return json.load(f)


def save(bets):
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(bets, f, indent=2)
        f.write("\n")


def _final_score(game_pk):
    """(away_runs, home_runs) once a game is Final, else None."""
    r = requests.get(f"{BASE}/game/{game_pk}/linescore", timeout=15)
    r.raise_for_status()
    d = r.json()
    teams = d.get("teams") or {}
    away = (teams.get("away") or {}).get("runs")
    home = (teams.get("home") or {}).get("runs")
    if away is None or home is None:
        return None
    return away, home


def grade(bet, score):
    """Settle one bet against a final score. Returns (result, detail).

    A half-point line cannot push, which is the whole reason books use it --
    but whole numbers can, so PUSH is handled rather than assumed away.
    """
    away, home = score
    side_is_away = bet.get("side") == "away"

    if bet["market"] == "spread":
        # A -10.5 line means the side must win by MORE than 10.5.
        margin = (away - home) if side_is_away else (home - away)
        need = -bet["line"]
        if margin > need:
            return "WON", f"won by {margin}, needed more than {need:g}"
        if margin == need:
            return "PUSH", f"won by exactly {margin}"
        return "LOST", f"won by {margin}, needed more than {need:g}"

    if bet["market"] == "total":
        total = away + home
        line = bet["line"]
        over = bet.get("side") == "over"
        if total == line:
            return "PUSH", f"total landed exactly on {line:g}"
        hit = (total > line) if over else (total < line)
        return ("WON" if hit else "LOST"), \
            f"total {total}, needed {'over' if over else 'under'} {line:g}"

    if bet["market"] == "moneyline":
        won = (away > home) if side_is_away else (home > away)
        return ("WON" if won else "LOST"), f"final {away}-{home}"

    return "UNKNOWN", f"unrecognised market '{bet['market']}'"


def payout(bet):
    """Profit on a won bet, from American odds. None when stake or price is
    unrecorded -- never guessed, because a fabricated P&L is worse than a
    blank one."""
    stake, price = bet.get("stake"), bet.get("price")
    if stake is None or price is None:
        return None
    if bet.get("result") == "WON":
        return round(stake * (price / 100.0 if price > 0 else 100.0 / abs(price)), 2)
    if bet.get("result") == "LOST":
        return -float(stake)
    if bet.get("result") == "PUSH":
        return 0.0
    return None


def settle(bets=None, write=True):
    """Grade every ungraded bet whose game is final. Returns the ledger."""
    bets = bets if bets is not None else load()
    for bet in bets:
        if bet.get("result") in ("WON", "LOST", "PUSH"):
            continue
        try:
            score = _final_score(bet["game_pk"])
        except Exception as e:
            bet["grade_error"] = str(e)
            continue
        if not score:
            continue
        result, detail = grade(bet, score)
        bet["result"] = result
        bet["result_detail"] = detail
        bet["final_away"], bet["final_home"] = score
        bet["profit"] = payout(bet)
    if write:
        save(bets)
    return bets


def summary(bets=None):
    """Record and P&L. Bets missing a stake are counted but not priced, and
    that is stated rather than hidden in an average."""
    bets = bets if bets is not None else load()
    graded = [b for b in bets if b.get("result") in ("WON", "LOST", "PUSH")]
    won = sum(1 for b in graded if b["result"] == "WON")
    lost = sum(1 for b in graded if b["result"] == "LOST")
    push = sum(1 for b in graded if b["result"] == "PUSH")
    priced = [b for b in graded if b.get("profit") is not None]
    decided = won + lost
    return {
        "total": len(bets),
        "graded": len(graded),
        "pending": len(bets) - len(graded),
        "won": won, "lost": lost, "push": push,
        "win_pct": round(100 * won / decided, 1) if decided else None,
        "profit": round(sum(b["profit"] for b in priced), 2) if priced else None,
        "unpriced": len(graded) - len(priced),
        "by_trigger": _by_trigger(graded),
    }


def _by_trigger(graded):
    """Win rate split by what prompted the bet. The point of the whole ledger:
    does the golden signal actually beat the other reasons you bet?"""
    out = {}
    for b in graded:
        t = b.get("trigger") or "manual"
        row = out.setdefault(t, {"won": 0, "lost": 0, "push": 0})
        row[b["result"].lower()] = row.get(b["result"].lower(), 0) + 1
    for t, row in out.items():
        decided = row["won"] + row["lost"]
        row["win_pct"] = round(100 * row["won"] / decided, 1) if decided else None
    return out
