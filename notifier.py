"""Push notifications via ntfy.sh (free, no account/API key needed).

Install the ntfy app (iOS/Android) or visit ntfy.sh/<your-topic> in a browser,
subscribe to the topic name set in NTFY_TOPIC, and you'll get a push the moment
this bot posts to it.
"""
import os
import requests

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_URL = "https://ntfy.sh"


def send_alert(title, message, priority="urgent", tags="baseball,rotating_light"):
    if not NTFY_TOPIC:
        print("[notifier] NTFY_TOPIC not set — printing alert instead of sending push:")
        print(f"  {title}\n  {message}")
        return
    try:
        requests.post(
            f"{NTFY_URL}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                # ntfy titles must be latin-1-safe as plain headers; encode to
                # UTF-8 bytes so emoji/non-ASCII titles (e.g. the bullpen-
                # exhausted tier's siren) don't crash the request.
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": tags,
            },
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[notifier] Failed to send push notification: {e}")
