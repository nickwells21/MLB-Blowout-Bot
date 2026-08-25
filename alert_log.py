"""Append-only log of fired alerts, read by dashboard.html to show scenario history."""
import json
import os

import paths

LOG_FILE = paths.data_path("alerts_log.json")
MAX_ENTRIES = 500


def append(record):
    entries = _load()
    entries.append(record)
    entries = entries[-MAX_ENTRIES:]
    with open(LOG_FILE, "w") as f:
        json.dump(entries, f, indent=2)


def _load():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r") as f:
        return json.load(f)
