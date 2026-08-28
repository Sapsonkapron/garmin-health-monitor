#!/usr/bin/env python3
"""
Нагадування про тренування з рандомізованими повідомленнями.
Використання: python training_reminder.py --type gravel [--telegram]
"""

import os
import sys
import json
import random
import argparse
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import date

# --- Повідомлення Gravel ---
GRAVEL_MESSAGES = [
    "Сьогодні день гравелу! 🚴 60-90 хв у зоні Z2 (118-137 bpm). Їдь з годинником — Garmin порахує VO2max.",
    "Gravel day! 🚴 Тримай пульс 118-137 bpm (Z2), 60-90 хв. Годинник на руці = VO2max трекінг.",
    "Час крутити педалі! 🚴 Сьогодні гравел: Z2 зона, 118-137 bpm, 60-90 хвилин. Garmin все запише.",
    "Гравел сьогодні! 🚴 План: 60-90 хв, зона Z2 (пульс 118-137). Їдь рівно, без ривків — це база для VO2max.",
    "На велосипед! 🚴 Сьогодні easy ride: 60-90 хв у Z2 (118-137 bpm). Спокійно, без героїзму. Garmin зафіксує прогрес.",
]

MESSAGES = {
    "gravel": GRAVEL_MESSAGES,
}

STATE_FILE = Path("/tmp/garmin_data/reminder_state.json")


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


def already_sent_today(reminder_type: str) -> bool:
    state = _read_state()
    return state.get(reminder_type) == date.today().isoformat()


def mark_sent_today(reminder_type: str):
    state = _read_state()
    state[reminder_type] = date.today().isoformat()
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
    parser = argparse.ArgumentParser(description="Нагадування про тренування")
    parser.add_argument(
        "--type",
        required=True,
        choices=["gravel"],
        help="Тип тренування: gravel",
    )
    parser.add_argument("--telegram", action="store_true", help="Примусово відправити повідомлення в Telegram (ігнорує захист від дублювання)")
    args = parser.parse_args()

    messages = MESSAGES[args.type]
    message = random.choice(messages)
    print(message)

    # Захист від дублювання в той самий день (для scheduled runs без --telegram)
    should_send = args.telegram or not already_sent_today(args.type)
    if not should_send:
        print("Вже відправлено сьогодні — пропускаємо")
        return

    if send_telegram(message):
        mark_sent_today(args.type)
        print("✅ Відправлено в Telegram")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
