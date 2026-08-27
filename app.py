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

from flask import Flask, send_from_directory

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
    bot.notify_startup()
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


@app.route("/status.json")
def status_json():
    return send_from_directory(paths.DATA_DIR, "status.json")


@app.route("/alerts_log.json")
def alerts_json():
    return send_from_directory(paths.DATA_DIR, "alerts_log.json")


@app.route("/odds_snapshot.json")
def odds_json():
    return send_from_directory(paths.DATA_DIR, "odds_snapshot.json")


@app.route("/live_snapshot.json")
def live_json():
    return send_from_directory(paths.DATA_DIR, "live_snapshot.json")


start_bot_thread()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8090))
    app.run(host="0.0.0.0", port=port, threaded=True)
