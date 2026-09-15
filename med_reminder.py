#!/usr/bin/env python3
"""
Вечірнє нагадування про прийом ліків (20:00).
Без назв препаратів — коротке повідомлення + ротаційний рядок мотивації.
"""

import os
import sys
import json
import argparse
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import assistant_content as ac

STATE_FILE = Path("/tmp/garmin_data/med_state.json")
KYIV = ZoneInfo("Europe/Kyiv")


def load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"⚠️ Не вдалось зберегти стан: {e}", file=sys.stderr)


def already_sent_today() -> bool:
    return _read_state().get("evening_med") == datetime.now(KYIV).date().isoformat()


def mark_sent_today():
    state = _read_state()
    state["evening_med"] = datetime.now(KYIV).date().isoformat()
    _write_state(state)


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("❌ TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID не налаштовані", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4000]}
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True
    except Exception as e:
        print(f"❌ Помилка відправки в Telegram: {e}", file=sys.stderr)
        return False


def main():
    load_env()
    parser = argparse.ArgumentParser(description="Вечірнє нагадування про ліки")
    args = parser.parse_args()

    message = (
        "💊 Вечірній прийом ліків — не забудь\n"
        f"{ac.tip_of_the_day(ac.MED_EVENING_LINES)}"
    )
    print(message)

    if already_sent_today():
        print("Вже відправлено сьогодні — пропускаємо")
        return

    if send_telegram(message):
        mark_sent_today()
        print("✅ Відправлено в Telegram")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
