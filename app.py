"""
Web entrypoint for deployment (Railway, etc).

Runs the same polling loop as bot.py, but in a background thread inside a
Flask process so a single deployed service both watches games and serves
the dashboard over HTTP. For local development you can still run `python
bot.py` standalone with no web server at all.
"""
import os
import threading
import time

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, send_from_directory

import bot
import paths
import state

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_started = False
_started_lock = threading.Lock()


def _run_bot_loop():
    print(
        f"[app] Background bot loop starting. Run-diff bar: "
        f"{bot.RUN_DIFF_MID} early / {bot.RUN_DIFF_LATE} from the 7th, "
        f"polling every {bot.POLL_INTERVAL_SECONDS}s. "
        f"Game-window scheduling: {'on' if bot.SCHEDULE_ENABLED else 'off'}."
    )
    alerted = state.load_alerted()
    # run_loop owns its own error handling and sleeping; if it ever escapes,
    # restart it rather than letting the thread die silently.
    while True:
        try:
            bot.run_loop(alerted)
        except Exception as e:
            print(f"[app] Bot loop crashed, restarting in 30s: {e}")
            time.sleep(30)


def start_bot_thread():
    global _started
    with _started_lock:
        if _started:
            return
        _started = True
        threading.Thread(target=_run_bot_loop, daemon=True).start()


@app.route("/")
def dashboard():
    return send_from_directory(BASE_DIR, "dashboard.html")


def _serve_json(name, empty):
    """Serve a data file, falling back to an empty document when it does not
    exist yet.

    These files are written lazily: alerts_log.json in particular is only
    created when the first position-player alert fires, which on a fresh
    volume can be days. A 404 there forces every client to special-case a
    missing file, so serve the empty shape instead and keep the contract
    "these endpoints always return valid JSON"."""
    path = os.path.join(paths.DATA_DIR, name)
    if not os.path.exists(path):
        return jsonify(empty)
    return send_from_directory(paths.DATA_DIR, name)


@app.route("/status.json")
def status_json():
    return _serve_json("status.json", {"bot_state": "starting", "live_games": 0})


@app.route("/alerts_log.json")
def alerts_json():
    return _serve_json("alerts_log.json", [])


@app.route("/odds_snapshot.json")
def odds_json():
    return _serve_json("odds_snapshot.json", {"games": []})


@app.route("/bets.json")
def bets_json():
    """The bet ledger, graded live.

    Served from the repo rather than DATA_DIR, and settled on read with
    write=False: DATA_DIR is not a persisted volume (a golden alert's log entry
    was lost to a redeploy on 2026-08-27), so anything written at runtime is
    gone on the next ship. Grading on read means the results are always correct
    without depending on storage that does not survive.
    """
    try:
        import bets
        ledger = bets.settle(write=False)
        return jsonify({"summary": bets.summary(ledger), "bets": ledger})
    except Exception as e:
        return jsonify({"error": str(e), "bets": []}), 200


@app.route("/live_snapshot.json")
def live_json():
    return _serve_json("live_snapshot.json", {"games": []})


start_bot_thread()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8090))
    app.run(host="0.0.0.0", port=port, threaded=True)
