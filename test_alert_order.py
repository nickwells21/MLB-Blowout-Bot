"""Logic-level tests for notification ORDER and CONTENT.

No network, no ntfy, no state files: mlb_api / notifier / odds / alert_log are
replaced with stubs before bot.py is imported, and check_game() is driven
through hand-built boxscores.

The thing under test is the property the user reported broken -- that the push
sent LAST is the one they see, so the most important alert has to be the last
one out. Every scenario asserts that invariant, plus the specific behaviour it
is about.

Run:  python test_alert_order.py
"""
import sys
import types


# --------------------------------------------------------------------------
# Stubs, installed before bot.py imports them
# --------------------------------------------------------------------------

SENT = []          # [(title, message, priority, tags)]
ALERT_LOG = []
ROSTER = {}        # person_id -> position abbreviation


def _stub_notifier():
    m = types.ModuleType("notifier")

    def send_alert(title, message, priority="urgent", tags="baseball"):
        SENT.append((title, message, priority, tags))

    m.send_alert = send_alert
    return m


def _stub_odds():
    m = types.ModuleType("odds")
    m.BOOK = "teststub"
    m.fetch_for_alert = lambda home, away: None
    m.get_cached_or_fetch = lambda: None
    m.get_alternates_for_event = lambda eid: None
    m.find_game = lambda all_odds, home, away: None
    m.extract_for_book = lambda match, alt, book: None
    m.quota_status = lambda: None
    m.maybe_refresh_snapshot = lambda *a, **k: None
    return m


def _stub_alert_log():
    m = types.ModuleType("alert_log")
    m.append = lambda record: ALERT_LOG.append(record)
    return m


def _stub_mlb_api():
    """Mirrors the real module's contract against the fixtures below."""
    m = types.ModuleType("mlb_api")

    def is_position_player(person_id, fallback_abbr=None):
        abbr = ROSTER.get(person_id, fallback_abbr)
        return abbr not in ("P", "TWP") if abbr else False

    def count_pitchers_used(boxscore, side):
        return len(boxscore["teams"][side].get("pitchers", []))

    def get_linescore(boxscore):
        out = {}
        for side in ("away", "home"):
            team = boxscore["teams"][side]
            out[side] = team["teamStats"]["batting"]["runs"]
            out[f"{side}_name"] = team["team"]["name"]
        return out

    def find_position_players_pitching(boxscore):
        hits = []
        for side in ("away", "home"):
            team = boxscore["teams"][side]
            for pid in team.get("pitchers", []):
                player = team["players"][f"ID{pid}"]
                abbr = ROSTER.get(player["person"]["id"])
                if abbr not in ("P", "TWP"):
                    hits.append({
                        "side": side,
                        "player_id": pid,
                        "name": player["person"]["fullName"],
                        "real_position": abbr,
                    })
        return hits

    def get_bullpen_detail(boxscore, side):
        team = boxscore["teams"][side]
        pids = team.get("pitchers", [])
        detail = []
        for idx, pid in enumerate(pids):
            player = team["players"][f"ID{pid}"]
            abbr = ROSTER.get(player["person"]["id"], "P")
            detail.append({
                "player_id": pid,
                "name": player["person"]["fullName"],
                "position": abbr,
                "is_position_player": abbr not in ("P", "TWP"),
                "innings_pitched": player["stats"]["pitching"].get("inningsPitched"),
                "pitches": player["stats"]["pitching"].get("numberOfPitches"),
                "is_current": idx == len(pids) - 1,
            })
        return detail

    def get_starter_info(boxscore, side):
        team = boxscore["teams"][side]
        pids = team.get("pitchers", [])
        if not pids:
            return None
        p = team["players"][f"ID{pids[0]}"]
        return {
            "player_id": pids[0],
            "name": p["person"]["fullName"],
            "innings_pitched": p["stats"]["pitching"].get("inningsPitched"),
        }

    m.is_position_player = is_position_player
    m.count_pitchers_used = count_pitchers_used
    m.get_linescore = get_linescore
    m.find_position_players_pitching = find_position_players_pitching
    m.get_bullpen_detail = get_bullpen_detail
    m.get_starter_info = get_starter_info
    m.get_boxscore = lambda pk: (_ for _ in ()).throw(AssertionError("patched per test"))
    m.get_linescore_detail = lambda pk: (_ for _ in ()).throw(AssertionError("patched per test"))
    m.get_live_game_pks = lambda d: []
    m.positions_refreshed_at = lambda: None
    m.refresh_league_positions = lambda: None
    return m


sys.modules["notifier"] = _stub_notifier()
sys.modules["odds"] = _stub_odds()
sys.modules["alert_log"] = _stub_alert_log()
sys.modules["mlb_api"] = _stub_mlb_api()

import bot  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

GAME_PK = 777001


def pitcher(pid, name, position="P", ip="1.0", pitches=18):
    """One pitcher row. `position` is the ROSTER position -- '3B' makes this a
    position player on the mound, which is the whole point of the bot."""
    ROSTER[pid] = position
    return {
        "person": {"id": pid, "fullName": name},
        "position": {"abbreviation": "P"},   # boxscore always claims "P"
        "stats": {"pitching": {"inningsPitched": ip, "numberOfPitches": pitches}},
    }


def boxscore(away_runs, home_runs, away_pitchers, home_pitchers,
             away_name="Rockies", home_name="Dodgers"):
    def team(name, runs, pitchers):
        return {
            "team": {"name": name},
            "teamStats": {"batting": {"runs": runs}},
            "pitchers": [p["person"]["id"] for p in pitchers],
            "players": {f"ID{p['person']['id']}": p for p in pitchers},
        }

    return {
        "teams": {
            "away": team(away_name, away_runs, away_pitchers),
            "home": team(home_name, home_runs, home_pitchers),
        }
    }


def arms(prefix, n, position="P"):
    return [pitcher(int(f"{prefix}{i}"), f"{'Arm' if position == 'P' else 'Fielder'} {prefix}{i}",
                    position=position) for i in range(1, n + 1)]


def poll(box, inning, inning_state, alerted, outs=1, ordinal=None):
    """Drive one check_game() with a given boxscore and inning state."""
    bot.mlb_api.get_boxscore = lambda pk: box
    bot.mlb_api.get_linescore_detail = lambda pk: {
        "inning": inning,
        "inning_ordinal": ordinal or f"{inning}th",
        "inning_state": inning_state,
        "outs": outs,
        "home_runs": box["teams"]["home"]["teamStats"]["batting"]["runs"],
        "away_runs": box["teams"]["away"]["teamStats"]["batting"]["runs"],
    }
    return bot.check_game(GAME_PK, alerted)


def priority_map(sent):
    """kind -> ntfy priority. SENT and the kinds poll() returns are parallel
    lists in the same send order, so they zip."""
    return {kind: SENT[i][2] for i, kind in enumerate(sent)}


def reset(seeding=False):
    """Fresh state for one scenario.

    `seeding` models a bot that has just started: its first poll adopts
    whatever is already in progress instead of alerting on it. Every scenario
    except the restart ones wants seeding OFF, i.e. a bot that has been
    running and is seeing a genuine new transition.
    """
    SENT.clear()
    ALERT_LOG.clear()
    ROSTER.clear()
    bot._last_half_inning.clear()
    bot._last_pitcher_count.clear()
    bot._last_digest_lead.clear()
    bot._seeding = bool(seeding)
    return set()


# --------------------------------------------------------------------------
# Assertions
# --------------------------------------------------------------------------

FAILURES = []
PASSES = []


def check(condition, label, detail=""):
    if condition:
        PASSES.append(label)
        print(f"  PASS  {label}")
    else:
        FAILURES.append((label, detail))
        print(f"  FAIL  {label}\n        {detail}")


def ranks_ascending(sent_kinds):
    """The core invariant: pushes leave in non-decreasing importance, so the
    last one out -- the one on top of the phone -- is the most important."""
    ranks = [bot.ALERT_RANK.get(k, bot.UNRANKED_ALERT_RANK) for k in sent_kinds]
    return all(a <= b for a, b in zip(ranks, ranks[1:])), list(zip(sent_kinds, ranks))


def titles():
    return [t for t, _m, _p, _g in SENT]


def body(index):
    return SENT[index][1]


def enter_urgency(alerted):
    """Get a game into urgency mode: lead 6, trailing team 2 relievers deep.
    Returns the poll's sent kinds."""
    box = boxscore(2, 8, arms("1", 3), arms("2", 1))   # away trailing, 2 relievers
    return poll(box, 5, "Top", alerted)


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

def scenario_reported_bug():
    """THE REPORTED BUG: inning flips and the bullpen ladder trips on the same
    poll. The inning push must never land on top of the bullpen push."""
    print("\n[1] inning change + bullpen rung on the same poll")
    alerted = reset()
    enter_urgency(alerted)
    SENT.clear()

    # Inning flips to Bottom 5th AND the trailing team goes to a 3rd reliever.
    box = boxscore(2, 8, arms("1", 4), arms("2", 1))
    sent = poll(box, 5, "Bottom", alerted)

    ok, pairs = ranks_ascending(sent)
    check(ok, "pushes leave in ascending importance", f"got {pairs}")
    check("bullpen_exhausted" in sent, "bullpen rung fired", f"got {sent}")
    check(sent[-1] == "bullpen_exhausted",
          "bullpen rung is the LAST push sent (top of the stack)", f"got {sent}")
    check("inning_digest" in sent,
          "inning report still sent -- a rung no longer suppresses it", f"got {sent}")
    check(sent.index("inning_digest") < sent.index("bullpen_exhausted"),
          "inning report sent BEFORE the rung, so the rung sits above it",
          f"got {sent}")
    last_body = SENT[-1][1]
    check("reliever(s) used" in last_body and "On the mound" in last_body,
          "bullpen push carries relievers-used + who is on the mound",
          repr(last_body))

    # And the suppressed inning boundary must not come back next poll.
    SENT.clear()
    again = poll(box, 5, "Bottom", alerted)
    check("inning_digest" not in again,
          "suppressed inning boundary does not re-fire on the next poll", f"got {again}")


def scenario_golden_on_top():
    """Position player enters on the same poll as an inning change and a lead
    change. Golden must be on top and must never be merged into anything."""
    print("\n[2] golden + inning change + lead change on one poll")
    alerted = reset()
    enter_urgency(alerted)
    SENT.clear()

    # Lead jumps 6 -> 9 (a new Big Lead bucket), inning flips, and the 3rd
    # reliever is a shortstop.
    away = arms("1", 3) + [pitcher(199, "Ezequiel Tovar", position="SS")]
    box = boxscore(2, 11, away, arms("2", 1))
    sent = poll(box, 5, "Bottom", alerted)

    ok, pairs = ranks_ascending(sent)
    check(ok, "pushes leave in ascending importance", f"got {pairs}")
    check("golden" in sent, "golden fired", f"got {sent}")
    check(sent[-1] == "golden", "golden is the LAST push sent (top of the stack)", f"got {sent}")
    check(sum(1 for k in sent if k == "golden") == 1, "exactly one golden push", f"got {sent}")
    golden_body = SENT[-1][1]
    check("Tovar" in golden_body and "SS" in golden_body,
          "golden push names the position player and his real position", repr(golden_body))
    check("Lead is up to" not in golden_body,
          "golden was NOT merged with the lead update", repr(golden_body))
    check(SENT[-1][2] == "urgent", "golden pushed at urgent priority", SENT[-1][2])
    check(len(ALERT_LOG) == 1, "golden logged once", f"{len(ALERT_LOG)} entries")


def scenario_midinning_lead_jump():
    """No inning boundary: a lead running away must still push promptly, on its
    own, not wait for the next inning report."""
    print("\n[3] lead jumps mid-inning (no inning change)")
    alerted = reset()
    enter_urgency(alerted)
    SENT.clear()

    # Same half-inning as the urgency-entry poll, lead 6 -> 9.
    box = boxscore(2, 11, arms("1", 3), arms("2", 1))
    sent = poll(box, 5, "Top", alerted)

    check("big_lead" in sent, "big lead fired mid-inning", f"got {sent}")
    check("inning_digest" not in sent, "no inning report mid-inning", f"got {sent}")
    check("Lead is up to 9 runs" in SENT[0][1],
          "big-lead push is its own notification with the new lead", repr(SENT[0][1]))

    # Extreme lead is escalation and must stay its own push even at a boundary.
    SENT.clear()
    box = boxscore(2, 13, arms("1", 3), arms("2", 1))
    sent = poll(box, 5, "Bottom", alerted)
    ok, pairs = ranks_ascending(sent)
    check(ok, "pushes leave in ascending importance", f"got {pairs}")
    check("extreme_lead" in sent, "extreme lead fired at the boundary", f"got {sent}")
    check(sent.index("extreme_lead") > sent.index("inning_digest")
          if "inning_digest" in sent else True,
          "extreme lead is sent after (above) the inning report", f"got {sent}")


def scenario_digest_consolidation():
    """A plain inning boundary with a new lead bucket produces ONE push."""
    print("\n[4] inning boundary consolidates lead + bullpen into one report")
    alerted = reset()
    enter_urgency(alerted)
    SENT.clear()

    box = boxscore(2, 11, arms("1", 3), arms("2", 1))   # lead 6 -> 9, new bucket
    sent = poll(box, 5, "Bottom", alerted)

    # Big Lead no longer folds in: folding would demote it to the report's
    # "low" priority, and it is one of the two alerts that must outrank it.
    check(sent == ["inning_digest", "big_lead"],
          "report and big lead both sent, report first", f"got {sent}")
    text = SENT[0][1]
    check("Bottom 5th" in SENT[0][0] or "5th" in SENT[0][0],
          "report titled with the half-inning", SENT[0][0])
    check("(lead 9)" in text, "report carries the current lead", repr(text))
    check("On the mound" in text and "reliever(s) used" in text,
          "report carries the trailing bullpen state", repr(text))
    check("Lead is up to 9 runs" not in text,
          "big lead NOT folded into the report", repr(text))
    prio = priority_map(sent)
    check(prio.get("inning_digest") == "low",
          "inning report is the quietest push", prio.get("inning_digest"))
    check(prio.get("big_lead") != "low",
          "big lead is louder than the inning report", prio.get("big_lead"))
    check("Lead +3 since the last report." in text,
          "report says how far the lead moved", repr(text))


def scenario_urgency_entry():
    """The previously-fixed double-ping: urgency entry must be one push."""
    print("\n[5] urgency-mode entry poll")
    alerted = reset()
    # Seed the inning + pitcher baselines so entry lands on a poll where an
    # inning change AND a pitcher change are both live.
    poll(boxscore(2, 3, arms("1", 2), arms("2", 1)), 4, "Top", alerted)
    SENT.clear()

    box = boxscore(2, 8, arms("1", 3), arms("2", 1))     # lead 6, 2 relievers
    sent = poll(box, 4, "Bottom", alerted)

    check(sent == ["urgency_on"], "urgency entry is exactly one push", f"got {sent}")
    check("inning_digest" not in sent, "inning report suppressed on the entry poll", f"got {sent}")
    check("pitcher_change" not in sent, "pitcher change suppressed on the entry poll", f"got {sent}")
    check("big_lead" not in sent, "big-lead push folded into the entry message", f"got {sent}")
    check("Lead is up to 6 runs" in SENT[0][1],
          "entry message carries the folded big-lead line", repr(SENT[0][1]))

    SENT.clear()
    again = poll(box, 4, "Bottom", alerted)
    check(again == [], "nothing re-fires on the following identical poll", f"got {again}")


def scenario_sabotage():
    """Every tier sabotaged one at a time. Golden must survive all of them."""
    print("\n[6] sabotage: each tier raises in turn, golden must still fire")
    tiers = [
        "check_urgency_mode", "check_bullpen_depth", "check_extreme_lead",
        "check_big_lead", "check_inning_report", "check_pitcher_change",
        "consume_inning_change",
    ]
    originals = {name: getattr(bot, name) for name in tiers}

    def boom(*a, **k):
        raise RuntimeError("sabotage")

    survived = 0
    for name in tiers:
        alerted = reset()
        enter_urgency(alerted)
        SENT.clear()
        setattr(bot, name, boom)
        try:
            away = arms("1", 3) + [pitcher(199, "Ezequiel Tovar", position="SS")]
            sent = poll(boxscore(2, 11, away, arms("2", 1)), 5, "Bottom", alerted)
        finally:
            setattr(bot, name, originals[name])
        ok = sent and sent[-1] == "golden"
        check(bool(ok), f"golden still fires and lands on top when {name} raises",
              f"got {sent}")
        survived += bool(ok)

    # Sabotage the dependencies golden itself leans on.
    for name, patch in (("linescore_detail", "detail"), ("odds", "odds"), ("alert_log", "log")):
        alerted = reset()
        enter_urgency(alerted)
        SENT.clear()
        saved = {}
        if patch == "detail":
            saved["d"] = bot.mlb_api.get_linescore_detail
        elif patch == "odds":
            saved["o"] = bot.odds.fetch_for_alert
            bot.odds.fetch_for_alert = boom
        else:
            saved["l"] = bot.alert_log.append
            bot.alert_log.append = boom
        try:
            away = arms("1", 3) + [pitcher(199, "Ezequiel Tovar", position="SS")]
            box = boxscore(2, 11, away, arms("2", 1))
            bot.mlb_api.get_boxscore = lambda pk: box
            if patch == "detail":
                bot.mlb_api.get_linescore_detail = boom
            else:
                bot.mlb_api.get_linescore_detail = lambda pk: {
                    "inning": 5, "inning_ordinal": "5th", "inning_state": "Bottom",
                    "outs": 1, "home_runs": 11, "away_runs": 2,
                }
            sent = bot.check_game(GAME_PK, alerted)
        finally:
            if patch == "odds":
                bot.odds.fetch_for_alert = saved["o"]
            elif patch == "log":
                bot.alert_log.append = saved["l"]
        ok = sent and sent[-1] == "golden"
        check(bool(ok), f"golden still fires and lands on top when {name} raises",
              f"got {sent}")
        survived += bool(ok)

    # One push failing must not drop the ones queued behind it.
    alerted = reset()
    enter_urgency(alerted)
    SENT.clear()
    real_send = bot.notifier.send_alert

    def flaky(title, message, priority="urgent", tags="baseball"):
        if "Inning Report" in title or "Big Lead" in title:
            raise RuntimeError("ntfy rate limited")
        real_send(title, message, priority=priority, tags=tags)

    bot.notifier.send_alert = flaky
    try:
        away = arms("1", 3) + [pitcher(199, "Ezequiel Tovar", position="SS")]
        sent = poll(boxscore(2, 11, away, arms("2", 1)), 5, "Bottom", alerted)
    finally:
        bot.notifier.send_alert = real_send
    check("golden" in sent and titles() and "GOLDEN" in titles()[-1],
          "a failing push does not drop the golden push queued behind it",
          f"sent={sent} titles={titles()}")

    print(f"  ({survived}/{len(tiers) + 3} sabotaged tiers left golden intact)")


def scenario_ladder_integrity():
    """Structural guards on the ladder itself."""
    print("\n[7] ordering ladder is data-driven and golden-safe")
    check(bot.ALERT_RANK["golden"] == max(bot.ALERT_RANK.values()),
          "golden holds the strictly highest rank",
          str(bot.ALERT_RANK))
    check(sum(1 for v in bot.ALERT_RANK.values() if v == bot.ALERT_RANK["golden"]) == 1,
          "nothing ties golden's rank", str(bot.ALERT_RANK))
    check(bot.ALERT_RANK["bullpen_exhausted"] > bot.ALERT_RANK["inning_digest"]
          and bot.ALERT_RANK["bullpen_critical"] > bot.ALERT_RANK["inning_digest"],
          "both bullpen rungs outrank the inning report (the reported bug)")
    check(bot.ALERT_RANK["pitcher_change"] > bot.ALERT_RANK["inning_digest"],
          "pitcher change outranks the inning report")
    check(bot.UNRANKED_ALERT_RANK < min(bot.ALERT_RANK.values()),
          "an unregistered tier sorts below every registered one")

    # A future tier added without a rank must not be able to bury golden.
    SENT.clear()
    bus = bot.AlertBus()
    bus.add("golden", "GOLDEN - Position Player Pitching", "body", priority="urgent")
    bus.add("brand_new_tier_someone_forgot", "New Thing", "body")
    order = bus.flush()
    check(order[-1] == "golden",
          "an unranked new tier is sent BEFORE golden, so it cannot bury it", f"got {order}")

    # Folding never loses an alert when the target isn't in the batch.
    SENT.clear()
    bus = bot.AlertBus()
    bus.add("big_lead", "Big Lead Watch", "header\n\nLead is up to 9 runs.",
            merge_into="inning_digest", merge_line="Lead is up to 9 runs.")
    order = bus.flush()
    check(order == ["big_lead"] and len(SENT) == 1,
          "a fold with no target emits on its own instead of vanishing", f"got {order}")


def scenario_restart_does_not_replay():
    """Every redeploy restarts the process. Conditions that were already true
    when it booted must be adopted silently instead of re-firing as though
    they just happened -- that replay was the single biggest source of
    notification volume. Golden is the one exemption."""
    print("\n[8] a restart adopts the slate instead of replaying it")

    # --- boot straight into a game already deep in a blowout ---
    alerted = reset(seeding=True)
    box = boxscore(2, 11, arms("1", 4), arms("2", 1))   # lead 9, 3 relievers
    sent = poll(box, 5, "Bottom", alerted)

    check(sent == [], "a restart mid-blowout sends nothing", f"got {sent}")
    check(len(alerted) > 0,
          "but the conditions were recorded, not ignored", f"got {len(alerted)}")

    # --- the very next poll is a normal one ---
    bot._seeding = False
    SENT.clear()
    before = set(alerted)
    resent = poll(box, 5, "Bottom", alerted)
    check(resent == [],
          "adopted conditions do not re-fire once seeding ends", f"got {resent}")
    check(set(alerted) == before, "no new keys from an unchanged board", "changed")

    # --- a genuine new transition after the restart still alerts ---
    SENT.clear()
    box2 = boxscore(2, 11, arms("1", 5), arms("2", 1))  # 4th reliever = critical
    after = poll(box2, 6, "Top", alerted)
    check(after != [], "a NEW transition after a restart still alerts", f"got {after}")
    check("bullpen_critical" in after,
          "the new bullpen rung fired", f"got {after}")

    # --- golden is exempt: a position player pitching at boot still pushes ---
    alerted = reset(seeding=True)
    away = arms("1", 3) + [pitcher(199, "Ezequiel Tovar", position="SS")]
    box3 = boxscore(2, 11, away, arms("2", 1))
    sent = poll(box3, 5, "Bottom", alerted)

    check("golden" in sent,
          "GOLDEN still fires on the first poll after a restart", f"got {sent}")
    check(sent == ["golden"],
          "and it is the ONLY thing that does", f"got {sent}")
    check(len(ALERT_LOG) == 1, "golden logged exactly once", f"got {len(ALERT_LOG)}")


def main():
    scenario_reported_bug()
    scenario_golden_on_top()
    scenario_midinning_lead_jump()
    scenario_digest_consolidation()
    scenario_urgency_entry()
    scenario_sabotage()
    scenario_ladder_integrity()
    scenario_restart_does_not_replay()

    print("\n" + "=" * 62)
    print(f"{len(PASSES)} passed, {len(FAILURES)} failed")
    for label, detail in FAILURES:
        print(f"  FAILED: {label}\n          {detail}")
    print("=" * 62)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
