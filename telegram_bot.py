#!/usr/bin/env python3
"""
Telegram бот для запису ваги та тиску в Garmin Connect.
Формати повідомлень:
  92.5 кг        → вага
  92 кг          → вага
  (130/80/72)    → тиск 130/80, пульс 72
  (130/80)       → тиск 130/80
Запуск локально: python telegram_bot.py
"""

import os
import re
import sys
import json
import time
import logging
import urllib.request
import urllib.parse
from pathlib import Path
from garminconnect import Garmin

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOG_DIR / "telegram_bot.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


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


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        logger.error(f"{name} не налаштований")
        sys.exit(1)
    return value


def send_message(chat_id: int, text: str) -> bool:
    token = get_env("TELEGRAM_BOT_TOKEN")
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
        logger.error(f"Помилка відправки: {e}")
        return False


def get_updates(offset: int = None):
    token = get_env("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    if offset is not None:
        url += f"?offset={offset + 1}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"Помилка отримання повідомлень: {e}")
        return {"ok": False}


def parse_weight(text: str):
    match = re.search(r"(\d+(?:\.\d+)?)\s*кг", text.lower())
    if match:
        return float(match.group(1))
    return None


def parse_bp(text: str):
    match = re.search(r"\((\d{2,3})/(\d{2,3})(?:/(\d{2,3}))?\)", text)
    if match:
        systolic = int(match.group(1))
        diastolic = int(match.group(2))
        pulse = int(match.group(3)) if match.group(3) else 0
        return systolic, diastolic, pulse
    return None


def get_garmin_credentials():
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        raise ValueError("GARMIN_EMAIL або GARMIN_PASSWORD не налаштовані")
    return email, password


def log_to_garmin(weight=None, bp=None):
    email, password = get_garmin_credentials()
    client = Garmin(email, password)
    client.login()

    results = []
    if weight is not None:
        client.add_weigh_in(weight=weight, unitKey="kg")
        results.append(f"⚖️ Вага {weight} кг — записано ✅")

    if bp is not None:
        systolic, diastolic, pulse = bp
        client.add_blood_pressure(systolic=systolic, diastolic=diastolic, pulse=pulse)
        pulse_str = f", пульс {pulse}" if pulse else ""
        results.append(f"🩸 Тиск {systolic}/{diastolic}{pulse_str} — записано ✅")

    return "\n".join(results)


def process_message(text: str) -> str:
    weight = parse_weight(text)
    bp = parse_bp(text)

    if weight is None and bp is None:
        return "❌ Не зрозумів. Надішли:\n• вагу: `92.5 кг`\n• тиск: `(130/80/72)` або `(130/80)`"

    try:
        result = log_to_garmin(weight=weight, bp=bp)
        return result
    except Exception as e:
        logger.error(f"Помилка запису в Garmin: {e}")
        return f"❌ Помилка запису в Garmin: {e}"


def main():
    load_env()
    get_env("TELEGRAM_BOT_TOKEN")
    get_env("TELEGRAM_CHAT_ID")
    get_env("GARMIN_EMAIL")
    get_env("GARMIN_PASSWORD")

    logger.info("Бот запущено")
    print("Бот запущено. Надсилай повідомлення у Telegram.")

    offset = None
    while True:
        updates = get_updates(offset)
        if updates.get("ok"):
            for update in updates.get("result", []):
                offset = update["update_id"]
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                text = message.get("text", "")
                if not text:
                    continue
                logger.info(f"Отримано: {text}")
                response = process_message(text)
                send_message(chat_id, response)
        time.sleep(5)


if __name__ == "__main__":
    main()
