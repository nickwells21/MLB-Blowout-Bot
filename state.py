"""Tracks which position-player-pitching events have already been alerted on,
so restarting the bot or re-polling the same game doesn't spam duplicate alerts.
"""
import json
import os

import paths

STATE_FILE = paths.data_path("state.json")


def load_alerted():
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r") as f:
        return set(json.load(f))


def save_alerted(alerted_set):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(alerted_set), f, indent=2)
