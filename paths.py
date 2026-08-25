"""Shared data-file location. Defaults to this project's own folder (local dev).

On Railway, set DATA_DIR to a mounted Volume's path so state/alerts survive
redeploys and restarts — otherwise they reset every deploy since the
container filesystem is ephemeral.
"""
import os

DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)


def data_path(filename):
    return os.path.join(DATA_DIR, filename)
